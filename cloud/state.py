"""Run the existing async state machines inside one device's Durable Object.

No threads or local filesystem. SQLite commits the AI intent before Agora join.
An eviction ends the old human call; device media must close before a new call.
Caller capabilities and recognition history intentionally remain memory-only.
"""
import asyncio
import json
import time

from stackchan_ai import Manager
from stackchan_calls import CallManager, LIVE


class SqlJournal:
    """SQLite read/write/clear journal, plus durable stopped-request tombstones."""
    def __init__(self, sql):
        self.sql = sql
        sql.exec('CREATE TABLE IF NOT EXISTS recovery (id INTEGER PRIMARY KEY CHECK (id=1), value TEXT NOT NULL)')
        sql.exec('CREATE TABLE IF NOT EXISTS stopped (request_id TEXT PRIMARY KEY, until_seconds REAL NOT NULL)')

    def recent(self):
        return {row.request_id: row.until_seconds for row in self.sql.exec(
            'SELECT request_id,until_seconds FROM stopped').toArray()}

    def save_recent(self, rows, now):
        self.sql.exec('DELETE FROM stopped WHERE until_seconds <= ?', now)
        for request_id, expiry in rows.items():
            if expiry > now:
                self.sql.exec('INSERT INTO stopped VALUES (?,?) ON CONFLICT(request_id) DO UPDATE SET until_seconds=excluded.until_seconds', request_id, expiry)

    def read(self):
        rows = self.sql.exec('SELECT value FROM recovery WHERE id=1').toArray()
        if not len(rows):
            return None
        row = json.loads(rows[0].value)
        if row is None:
            raise ValueError('invalid_recovery_journal')
        return row

    def write(self, row):
        self.sql.exec('INSERT INTO recovery (id,value) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',
                      json.dumps(row, separators=(',', ':')))

    def clear(self):
        self.sql.exec('DELETE FROM recovery WHERE id=1')


class CloudManager(Manager):
    def __init__(self, provider, token_factory, journal, *, clock=time.time):
        self._init_state(provider, token_factory, journal, clock=clock, recent=journal.recent())
        self.maintenance_task = None
        self._restore()

    def _call(self, coroutine, timeout=25):
        # Inherited public methods retain their input validation, then return an
        # awaitable to the DO request handler. No cross-thread submission.
        return coroutine

    async def _stop(self, session_id, request_id):
        result = await super()._stop(session_id, request_id)
        self.journal.save_recent(self._recent, self.clock())
        return result

    async def _cleanup(self, record):
        await super()._cleanup(record)
        self.journal.save_recent(self._recent, self.clock())

    def tick(self):
        rec = self._record
        if not rec:
            return
        if rec['state'] == 'active' and self.clock() >= min(rec['lease_until'], rec['expires_at']):
            rec['cancel'] = True
        if (rec['state'] == 'cleanup' or (rec['state'] == 'active' and rec['cancel'])):
            if self.maintenance_task is None or self.maintenance_task.done():
                self.maintenance_task = asyncio.create_task(self._cleanup(rec))

    def tasks(self):
        result = [self.maintenance_task]
        if self._record:
            result.append(self._record.get('task'))
        return [task for task in result if task is not None and not task.done()]


class CloudCallManager(CallManager):
    def __init__(self, ai, device_token_factory, browser_token_factory, *, clock=time.time, on_event=None):
        self._init_state(ai, device_token_factory, browser_token_factory,
                         clock=clock, watch_interval=1, on_event=on_event)
        self.recovered = False
        self.ai.start_guard = self._allow_ai_start

    def _allow_ai_start(self):
        return self.recovered and super()._allow_ai_start()

    def _online(self):
        return self.recovered and super()._online()

    async def _device_poll(self, value):
        # All old call capabilities are invalid after object reconstruction.
        # call:null makes a device with an old call close media; only its later
        # idle acknowledgement admits new traffic. Old AI is journal-cleaned.
        if (value['state'] in ('idle', 'transition') and not value['media_connected']
                and value['call_id'] == '' and value['ai_session_id'] == ''):
            self.recovered = True
        return await super()._device_poll(value)

    async def tick(self):
        self.ai.tick()
        await self._tick()

    def tasks(self):
        result = self.ai.tasks()
        for rec in self.records.values():
            result.extend(rec.get(key) for key in ('announcement_job', 'history_job', 'confirmation_job'))
        return [task for task in result if task is not None and not task.done()]

    def needs_alarm(self):
        return bool(self.ai._record or self.tasks() or (self.current and self.current['state'] in LIVE))


async def finish_tasks(tasks):
    """Attach existing bounded jobs to ctx.waitUntil without leaking errors."""
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
