"""Offline cloud state regressions: real SQLite, fake provider, no network/SDK.

Run from the experiment root: python3 -m unittest discover -s cloud/tests -v
These exercise the shared state machines; Workers runtime/alarm bindings still
need a separate runtime smoke test before deployment.
"""
import asyncio
from contextlib import nullcontext
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'service'))
sys.path.insert(0, str(ROOT / 'cloud'))
from caller_auth import CallerAuth
from stackchan_ai import FileJournal, Manager, SessionError
from state import CloudCallManager, CloudManager, SqlJournal, finish_tasks


class SQLite:
    """Expose the DO SQL cursor surface using Python's actual SQLite engine."""
    def __init__(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row

    def exec(self, statement, *bindings):
        cursor = self.connection.execute(statement, bindings)
        rows = [SimpleNamespace(**dict(row)) for row in cursor.fetchall()]
        self.connection.commit()
        return SimpleNamespace(toArray=lambda: rows)


class Provider:
    def __init__(self):
        self.starts, self.stops, self.recoveries, self.confirmations = [], [], [], []
        self.spoken, self.contents = [], {'contents': []}
        self.confirmed, self.fail_stop = True, False
        self.on_start = None
        self.stop_gate = None

    async def start(self, channel, name, ttl):
        if self.on_start:
            self.on_start(channel)
        self.starts.append(channel)
        return 'agent-' + str(len(self.starts))

    async def stop(self, agent_id):
        self.stops.append(agent_id)
        if self.stop_gate:
            await self.stop_gate.wait()
        if self.fail_stop:
            raise RuntimeError('test upstream unavailable')

    async def recover(self, channel):
        self.recoveries.append(channel)

    async def confirm_stopped(self, agent_id, channel):
        self.confirmations.append((agent_id, channel))
        return self.confirmed

    async def history(self, agent_id, channel):
        return self.contents

    async def speak(self, agent_id, channel, text):
        self.spoken.append((agent_id, channel, text))


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1800000000
        self.sql, self.provider = SQLite(), Provider()
        self.owners, self.grants = [], []
        self.ai, self.calls = self.owner()

    async def asyncTearDown(self):
        tasks = {task for _, calls in self.owners for task in calls.tasks()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.sql.connection.close()

    def owner(self):
        ai = CloudManager(self.provider, lambda *args: 'ai-test-token',
                          SqlJournal(self.sql), clock=lambda: self.now)
        calls = CloudCallManager(ai, self.device_token, self.browser_token,
                                 clock=lambda: self.now)
        self.owners.append((ai, calls))
        return ai, calls

    def device_token(self, channel, expiry):
        self.grants.append((channel, expiry))
        return 'human-test-token'

    @staticmethod
    def browser_token(channel, expiry):
        return {'appId': 'test-app', 'channel': channel, 'uid': '20002',
                'robotUid': '20001', 'token': 'browser-test-token',
                'expiresAt': expiry, 'ttl': 600}

    async def device(self, state='idle', call='', session='', connected=False, *, calls=None):
        return await (calls or self.calls).device_poll({'state': state,
            'call_id': call, 'ai_session_id': session, 'media_connected': connected})

    async def tick(self, *, calls=None):
        calls = calls or self.calls
        await calls.tick()
        await finish_tasks(calls.tasks())

    async def new_call(self, request='1' * 32, **kwargs):
        return await self.calls.create(request, 'Alex', **kwargs)

    async def accept(self, call):
        await self.calls.device_action(call['call_id'], 'accept')
        await self.tick()
        return await self.calls.device_action(call['call_id'], 'ready')

    def assert_error(self, context, code):
        self.assertEqual(context.exception.code, code)

    async def test_ai_intent_is_durable_before_join_and_recovers_after_eviction(self):
        await self.device()
        def check_intent(channel):
            row = SqlJournal(self.sql).read()
            self.assertEqual(row['channel'], channel)
            self.assertEqual(row['agent_id'], '')
        self.provider.on_start = check_intent
        original = await self.ai.start('request_ai_0001')
        row = SqlJournal(self.sql).read()
        self.assertEqual(row['session_id'], original['session_id'])
        self.assertEqual(row['agent_id'], 'agent-1')
        ai, calls = self.owner()  # Same persisted SQL, no inherited process memory.
        self.assertEqual(ai._record['state'], 'cleanup')
        self.assertTrue(calls.needs_alarm())
        self.assertFalse((await calls.status())['device_online'])
        await self.tick(calls=calls)
        self.assertEqual(self.provider.stops, ['agent-1'])
        self.assertIsNone(SqlJournal(self.sql).read())
        self.assertFalse(calls.needs_alarm())
        await self.device(calls=calls)
        with self.assertRaises(SessionError) as error:
            await ai.start('request_ai_0001')
        self.assert_error(error, 'request_already_stopped')
        again, again_calls = self.owner()
        await self.device(calls=again_calls)
        with self.assertRaises(SessionError) as error:
            await again.start('request_ai_0001')
        self.assert_error(error, 'request_already_stopped')

    async def test_unknown_join_recovery_keeps_alarm_and_quarantine_until_retry(self):
        SqlJournal(self.sql).write({'request_id': 'request_ai_lost',
            'session_id': 'a' * 32, 'channel': 'stackchan-ai-' + 'b' * 32,
            'agent_id': '', 'created_at': self.now, 'expires_at': self.now + 600})
        ai, calls = self.owner()
        await self.device(calls=calls)
        await self.tick(calls=calls)
        self.assertEqual(len(self.provider.recoveries), 1)
        self.assertTrue(calls.needs_alarm())
        self.assertIsNotNone(SqlJournal(self.sql).read())
        with self.assertRaises(SessionError) as error:
            await ai.start('request_ai_new1')
        self.assert_error(error, 'session_busy')
        self.now += 35
        await self.tick(calls=calls)
        self.assertEqual(len(self.provider.recoveries), 2)
        self.assertIsNone(ai._record)
        self.assertFalse(calls.needs_alarm())

    async def test_cleanup_failure_retries_and_tick_does_not_duplicate_live_task(self):
        await self.device()
        await self.ai.start('request_ai_0001')
        self.provider.fail_stop = True
        self.now += 45
        await self.tick()
        self.assertEqual(self.ai._record['state'], 'cleanup')
        self.assertTrue(self.calls.needs_alarm())
        self.provider.fail_stop = False
        self.provider.stop_gate = asyncio.Event()
        await self.calls.tick()
        pending = self.ai.maintenance_task
        await self.calls.tick()
        self.assertIs(self.ai.maintenance_task, pending)
        self.provider.stop_gate.set()
        await finish_tasks(self.calls.tasks())
        self.assertIsNone(self.ai._record)
        self.assertEqual(len(self.provider.stops), 2)
        await self.tick()  # At-least-once alarm delivery is harmless after cleanup.
        self.assertEqual(len(self.provider.stops), 2)

    async def test_restart_discards_old_human_capability_and_waits_for_idle_ack(self):
        await self.device()
        call = await self.new_call()
        await self.accept(call)
        await self.calls.device_action(call['call_id'], 'connected')
        ai, calls = self.owner()
        response = await self.device('human', call['call_id'], connected=True, calls=calls)
        self.assertIsNone(response['call'])
        self.assertFalse((await calls.status())['device_online'])
        with self.assertRaises(SessionError) as error:
            await ai.start('request_ai_new1')
        self.assert_error(error, 'human_call_in_progress')
        with self.assertRaises(SessionError) as error:
            await calls.create('2' * 32, 'Sam')
        self.assert_error(error, 'device_offline')
        with self.assertRaises(SessionError) as error:
            await calls.caller_poll(call['call_id'], call['access_token'])
        self.assert_error(error, 'forbidden')
        for value in (('idle', '', '', True), ('idle', '', 'a' * 32, False),
                      ('transition', call['call_id'], '', False)):
            await self.device(*value, calls=calls)
            self.assertFalse(calls.recovered)
        await self.device(calls=calls)
        self.assertTrue((await calls.status())['device_online'])
        replacement = await ai.start('request_ai_new1')
        self.assertTrue(replacement['whip_url'].startswith('https://'))

    async def test_ai_to_human_to_ai_keeps_private_grants_behind_cleanup_and_ack(self):
        await self.device()
        ai = await self.ai.start('request_ai_0001')
        await self.device('ai', session=ai['session_id'], connected=True)
        call = await self.new_call()
        self.assertIsNone(call['rtc'])
        await self.device('ringing', call['call_id'], ai['session_id'], True)
        await self.tick()
        self.assertEqual(len(self.provider.spoken), 1)
        self.now += 1
        self.provider.contents = {'contents': [{'role': 'user', 'content': 'Answer.',
            'speech_start_ms': self.now * 1000}]}
        await self.tick()
        self.assertEqual(self.calls.current['state'], 'accepting')
        with self.assertRaises(SessionError) as error:
            await self.calls.device_action(call['call_id'], 'ready')
        self.assert_error(error, 'cleanup_pending')
        self.assertEqual(self.grants, [])
        self.provider.confirmed = False
        await self.ai.stop(session_id=ai['session_id'])
        await self.tick()
        with self.assertRaises(SessionError):
            await self.calls.device_action(call['call_id'], 'ready')
        self.assertIsNone((await self.calls.caller_poll(call['call_id'], call['access_token']))['rtc'])
        self.provider.confirmed = True
        self.now += 1
        await self.tick()
        ready = await self.calls.device_action(call['call_id'], 'ready')
        self.assertIn('stackchan-human-', ready['grant']['whip_url'])
        self.assertNotEqual(self.grants[0][0], self.provider.starts[0])
        await self.calls.device_action(call['call_id'], 'connected')
        await self.device('human', call['call_id'], connected=True)
        await self.calls.caller_end(call['call_id'], call['access_token'])
        with self.assertRaises(SessionError) as error:
            await self.ai.start('request_ai_0002')
        self.assert_error(error, 'human_call_in_progress')
        await self.device()
        resumed = await self.ai.start('request_ai_0002')
        self.assertNotEqual(resumed['session_id'], ai['session_id'])
        self.assertEqual(len(self.provider.starts), 2)

    async def test_ai_heartbeat_renews_lease_but_not_hard_expiry(self):
        await self.device()
        started = await self.ai.start('request_ai_0001')
        for _ in range(14):
            self.now += 40
            await self.ai.heartbeat(started['session_id'])
            await self.tick()
            self.assertTrue(self.calls.needs_alarm())
        self.now += 40
        await self.tick()
        self.assertIsNone(self.ai._record)
        self.assertFalse(self.calls.needs_alarm())
        self.assertEqual(self.provider.stops, ['agent-1'])

    async def test_caller_lease_expiry_ends_human_but_retains_device_ack_barrier(self):
        await self.device()
        call = await self.new_call()
        await self.accept(call)
        await self.calls.device_action(call['call_id'], 'connected')
        self.now += 15
        await self.tick()
        self.assertEqual(self.calls.current['reason'], 'caller_timeout')
        self.assertTrue((await self.calls.status())['busy'])
        self.assertFalse(self.calls.needs_alarm())
        await self.device('human', call['call_id'], connected=True)
        self.assertTrue((await self.calls.status())['busy'])
        await self.device()
        self.assertFalse((await self.calls.status())['busy'])

    async def test_device_lease_expires_even_when_caller_keeps_polling(self):
        await self.device()
        call = await self.new_call()
        await self.accept(call)
        await self.calls.device_action(call['call_id'], 'connected')
        self.now += 10
        await self.calls.caller_poll(call['call_id'], call['access_token'])
        self.now += 5
        await self.tick()
        self.assertEqual(self.calls.current['reason'], 'device_offline')

    async def test_ringing_deadline_expires_despite_fresh_both_side_leases(self):
        await self.device()
        call = await self.new_call()
        for _ in range(12):
            self.now += 5
            await self.calls.caller_poll(call['call_id'], call['access_token'])
            await self.device('ringing', call['call_id'])
            await self.tick()
        self.assertEqual(self.calls.current['reason'], 'ringing_timeout')
        self.assertEqual(self.grants, [])

    async def test_slow_join_history_origin_keeps_old_answer_closed_and_accepts_new_one(self):
        await self.device()
        def slow_start(_):
            self.now += 8
        self.provider.on_start = slow_start
        ai = await self.ai.start('request_ai_slow_join')
        provider_created = self.now
        self.now += 4
        await self.device('ai', session=ai['session_id'], connected=True)
        call = await self.new_call()
        self.provider.contents = {'start_ts': provider_created, 'contents': []}
        await self.device('ringing', call['call_id'], ai['session_id'], True)
        await self.tick()
        self.assertEqual(len(self.provider.spoken), 1)
        self.now += 1
        self.provider.contents = {'start_ts': provider_created, 'contents': [
            {'role': 'user', 'content': 'Answer.', 'speech_start_ms': 3000}]}
        await self.tick()
        self.assertEqual(self.calls.current['state'], 'ringing')
        self.now += 1
        self.provider.contents['contents'].append(
            {'role': 'user', 'content': 'Answer.', 'speech_start_ms': 6000})
        await self.tick()
        self.assertEqual(self.calls.current['state'], 'accepting')
        self.assertEqual(self.grants, [])  # AI cleanup still precedes human grant.

    async def test_stopped_request_tombstone_survives_restart_without_active_journal(self):
        await self.ai.stop(request_id='request_canceled')
        ai, calls = self.owner()
        await self.device(calls=calls)
        with self.assertRaises(SessionError) as error:
            await ai.start('request_canceled')
        self.assert_error(error, 'request_already_stopped')
        self.now += 600
        await ai.start('request_canceled')
        self.assertEqual(len(self.provider.starts), 1)

    async def test_guest_sessions_are_ephemeral_and_expiry_does_not_block_call_end(self):
        secret = 'i' * 43
        auth = CallerAuth(secret, clock=lambda: self.now, lock_factory=nullcontext)
        access = auth.exchange(secret, 'test-client')['access_token']
        await self.device()
        call = await self.new_call(caller_auth=auth, caller_session=access)
        restarted = CallerAuth(secret, clock=lambda: self.now, lock_factory=nullcontext)
        with self.assertRaises(SessionError) as error:
            restarted.authorize(access)
        self.assert_error(error, 'session_expired')
        # Session expiry and rotation cannot obstruct the existing capability.
        auth._sessions.clear()
        ended = await self.calls.caller_end(call['call_id'], call['access_token'])
        self.assertEqual(ended['call']['state'], 'ended')
        self.assertIsNone(ended['rtc'])

    async def test_file_and_sql_recovery_preserve_unknown_join_quarantine(self):
        row = {'request_id': 'request_ai_lost', 'session_id': 'a' * 32,
               'channel': 'stackchan-ai-' + 'b' * 32, 'agent_id': '',
               'created_at': self.now - 700, 'expires_at': self.now - 100}
        # Even expired intents must recover first. Local startup performs one
        # cleanup immediately; the cloud owner does it on its maintenance tick.
        SqlJournal(self.sql).write(row)
        cloud, calls = self.owner()
        await self.tick(calls=calls)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'recovery.json'
            journal = FileJournal(path)
            journal.write(row)
            local_provider = Provider()
            local = Manager(local_provider, lambda *args: 'test-token', path,
                            clock=lambda: self.now)
            try:
                self.assertEqual(local._record, cloud._record)
                self.assertEqual(local_provider.recoveries, [row['channel']])
                self.assertEqual(journal.read(), SqlJournal(self.sql).read())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.now += 36
                local.stop(request_id=row['request_id'])
                await cloud.stop(request_id=row['request_id'])
                self.assertIsNone(journal.read())
                self.assertIsNone(SqlJournal(self.sql).read())
                journal.clear()  # Already-cleared storage stays harmless.
                SqlJournal(self.sql).clear()
            finally:
                local.close()

    async def test_invalid_recovery_fails_closed_in_both_runtimes(self):
        for row in ({'channel': 'not-an-ai-channel'}, {'channel': 123}, [], 'bad', None):
            with self.subTest(row=row):
                SqlJournal(self.sql).write(row)
                with self.assertRaises(SessionError) as error:
                    self.owner()
                self.assert_error(error, 'invalid_recovery_journal')
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / 'recovery.json'
                    FileJournal(path).write(row)
                    with self.assertRaises(SessionError) as error:
                        Manager(Provider(), lambda *args: 'test-token', path)
                    self.assert_error(error, 'invalid_recovery_journal')


if __name__ == '__main__':
    unittest.main()
