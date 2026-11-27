"""Single-device local calls, coordinated on the existing AI owner's event loop.

No media, recognition text, credentials or call capabilities are persisted. The
only cloud jobs are a short ringing announcement, committed-history reads while
ringing, and confirmation that the old AI stopped. Human channels never start AI.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler
import json
import math
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit

from stackchan_ai import SessionError

LIVE = frozenset(('ringing', 'accepting', 'connecting', 'active'))
DEVICE_STATES = frozenset(('idle', 'ai', 'ringing', 'human', 'transition'))
ACCEPT = frozenset(('answer', 'answer the call', 'accept', 'accept the call',
                    'pick up', 'pick up the call', 'put him through', 'put her through'))
REJECT = frozenset(('decline', 'decline the call', 'reject', 'reject the call',
                    "don't answer", 'do not answer', 'no thanks'))
END_REASONS = frozenset(('caller_hangup', 'device_hangup', 'rejected', 'caller_cancel',
    'ringing_timeout', 'device_offline', 'caller_timeout', 'connecting_timeout',
    'call_expired', 'service_shutdown', 'credential_failed'))
EVENTS = frozenset(('ringing', 'announcement_sent', 'announcement_retry_failed',
    'history_retry_failed', 'voice_accept', 'voice_reject', 'connecting', 'active',
    'ended_error')) | frozenset('ended_' + reason for reason in END_REASONS)


def identifier(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[a-f0-9]{32}', value))


def decision(text):
    """Complete commands only: never substring/LLM inference or a bare yes."""
    if not isinstance(text, str) or len(text) > 80:
        return None
    value = text.lower().replace('\u2019', "'").strip()
    value = re.sub(r'[.!?,;:]+$', '', value).replace(',', ' ')
    words = value.split()
    if words and words[0] == 'please':
        words = words[1:]
    if words and words[-1] == 'please':
        words = words[:-1]
    value = ' '.join(words)
    if value in ACCEPT:
        return 'accept'
    if value in REJECT:
        return 'reject'
    return None


def field(value, key):
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def rows(history):
    contents = field(history, 'contents')
    if not isinstance(contents, list):
        raise ValueError('invalid_history')
    result = []
    for item in contents:
        role, text = field(item, 'role'), field(item, 'content')
        if role not in ('user', 'assistant'):
            continue
        # Even an empty/unavailable baseline item owns its timestamp identity.
        # If its content later fills in, it must remain excluded from this call.
        text = text if isinstance(text, str) else None
        times = []
        for key in ('speech_start_ms', 'speech_end_ms'):
            value = field(item, key)
            times.append(value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None)
        result.append((role, text, *times))
    return result


class HistoryCursor:
    """Timestamp identities survive mutable content; un-timed rows fail closed."""
    def __init__(self, baseline, since_ms, session_created_ms):
        self.previous = rows(baseline)
        self.since_ms, self.session_created_ms = since_ms, session_created_ms
        # Agora start_ts is epoch seconds; speech_* may be relative milliseconds.
        # Local created_at precedes the join request, so a slow join otherwise
        # shifts genuinely new speech backwards across the announcement cutoff.
        # Freeze a plausible provider origin from this baseline only. Missing,
        # malformed or out-of-window values retain the conservative local origin.
        start_ts = field(baseline, 'start_ts')
        if type(start_ts) is int and 1e9 <= start_ts < 1e11:
            provider_start_ms = start_ts * 1000
            if session_created_ms <= provider_start_ms <= since_ms:
                self.session_created_ms = provider_start_ms
        self.seen = {self._key(item) for item in self.previous}
        self.consumed_ids = {self._identity(item) for item in self.previous if item[2] is not None}

    @staticmethod
    def _key(item):
        return hashlib.sha256(json.dumps(item, ensure_ascii=False).encode()).digest()

    @staticmethod
    def _identity(item):
        return item[0], item[2]  # Role + stable speech start, never content/end.

    def _overlap_key(self, item):
        return ('start', *self._identity(item)) if item[2] is not None else ('row', self._key(item))

    def _fresh(self, stamp):
        epoch_ms = stamp if stamp >= 1e12 else self.session_created_ms + stamp
        return epoch_ms >= self.since_ms

    def commands(self, history):
        current = rows(history)
        previous = self.previous
        result = []
        # A cloud history row can first appear empty, then acquire its committed
        # text; assistant content/end may also fill in. Stable starts establish
        # freshness independently of the sliding cache's overlap or mutations.
        for item in current:
            role, text, start, _ = item
            if role != 'user' or start is None:
                continue
            identity = self._identity(item)
            if identity in self.consumed_ids:
                continue
            if not self._fresh(start):
                self.consumed_ids.add(identity)
                continue
            if not isinstance(text, str) or not text.strip():
                continue  # A new, post-cutoff empty row may still fill in.
            command = decision(text)
            if command:
                self.consumed_ids.add(identity)
                result.append(command)

        previous_keys = [self._overlap_key(item) for item in previous]
        current_keys = [self._overlap_key(item) for item in current]
        if current == previous or (current and len(current) < len(previous) and previous_keys[:len(current)] == current_keys):
            return result  # Keep the old cursor for an unchanged/stale prefix.
        if not previous:
            added = current
        else:
            overlap = next((n for n in range(min(len(previous), len(current)), 0, -1)
                            if previous_keys[-n:] == current_keys[:n]), 0)
            if not overlap:
                self.previous = current
                self.seen.update(self._key(item) for item in current)
                return result  # Only timestamp-established new users may act.
            added = current[overlap:]
        self.previous = current
        for item in added:
            key = self._key(item)
            if key in self.seen:
                continue
            self.seen.add(key)
            role, text, start, end = item
            if role != 'user' or start is not None:
                continue
            if end is not None and not self._fresh(end):
                continue
            command = decision(text)
            if command:
                result.append(command)
        return result


class CallManager:
    """All mutable state belongs to ai._loop; HTTP methods never await cloud I/O."""
    def __init__(self, ai, device_token_factory, browser_token_factory, *,
                 clock=time.time, watch_interval=0.25, on_event=None):
        self._init_state(ai, device_token_factory, browser_token_factory,
                         clock=clock, watch_interval=watch_interval, on_event=on_event)
        self.ai._call(self._initialize(), timeout=2)

    def _init_state(self, ai, device_token_factory, browser_token_factory, *,
                    clock=time.time, watch_interval=0.25, on_event=None):
        """Shared state only; the owner starts its local watchdog or cloud alarm."""
        self.ai, self.provider = ai, ai.provider
        self.device_token_factory, self.browser_token_factory = device_token_factory, browser_token_factory
        self.clock, self.watch_interval = clock, watch_interval
        self.on_event = on_event
        self.current = None
        self.records = {}
        self.requests = {}
        self.last_poll = None
        self.device = None
        self.closed = False

    async def _initialize(self):
        self.ai.start_guard = self._allow_ai_start
        self.watchdog = asyncio.create_task(self._watch())

    def _allow_ai_start(self):
        rec = self.current
        return not self.closed and not (rec and (
            rec['state'] in ('accepting', 'connecting', 'active')
            or (rec['needs_device_ack'] and rec['issued_human'])))

    def _busy(self):
        return bool(self.current and (self.current['state'] in LIVE or self.current['needs_device_ack']))

    def _emit(self, event):
        # The optional sink receives one fixed enum, never a record or payload.
        if event in EVENTS and self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                pass  # Diagnostics must not change call/privacy transitions.

    def _call(self, coroutine):
        return self.ai._call(coroutine, timeout=2)

    def status(self):
        return self._call(self._status())

    def create(self, request_id, caller_name, *, caller_auth=None, caller_session=None):
        if not identifier(request_id):
            raise SessionError('invalid_request_id', 400)
        if not isinstance(caller_name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z '\-]{0,31}", caller_name):
            raise SessionError('invalid_caller_name', 400)
        return self._call(self._create(request_id, caller_name.strip(), caller_auth, caller_session))

    def caller_poll(self, call_id, capability):
        return self._call(self._caller_poll(call_id, capability))

    def caller_end(self, call_id, capability):
        return self._call(self._caller_end(call_id, capability))

    def device_poll(self, value):
        if (not isinstance(value, dict) or set(value) != {'state', 'ai_session_id', 'call_id', 'media_connected'}
                or not isinstance(value.get('state'), str) or value['state'] not in DEVICE_STATES
                or type(value.get('media_connected')) is not bool
                or not isinstance(value.get('ai_session_id'), str)
                or (value['ai_session_id'] != '' and not self.ai._identifier(value['ai_session_id']))
                or (value.get('call_id') != '' and not identifier(value.get('call_id')))):
            raise SessionError('invalid_request', 400)
        return self._call(self._device_poll(dict(value)))

    def device_action(self, call_id, action):
        if not identifier(call_id) or action not in ('accept', 'reject', 'ready', 'connected', 'end'):
            raise SessionError('invalid_request', 400)
        return self._call(self._device_action(call_id, action))

    def close(self):
        self._call(self._shutdown())

    def _snapshot(self, rec):
        return {'id': rec['id'], 'caller': rec['caller'], 'state': rec['state'], 'reason': rec['reason']}

    def _device_response(self, rec=None, grant=None):
        result = {'server_time': int(self.clock()), 'call': self._snapshot(rec) if rec else None}
        if grant is not None:
            # A retried ready preserves its channel/token/deadline but must not
            # set the board's clock backwards to the original issue time.
            result['grant'] = dict(grant, server_time=int(self.clock()))
        return result

    def _caller_response(self, rec):
        rtc = dict(rec['rtc']) if rec['state'] in ('connecting', 'active') else None
        if rtc is not None:
            rtc['ttl'] = max(0, min(600, rec['expires_at'] - int(self.clock())))
        return {'call': self._snapshot(rec), 'rtc': rtc}

    def _online(self):
        return self.last_poll is not None and self.clock() - self.last_poll < 8

    def _live(self, rec):
        return not self.closed and self.current is rec and rec['state'] in LIVE

    def _capture_ai(self, rec):
        source = self.ai._record
        if source:
            # Keep the actual owned record, not just its currently-empty ID:
            # an in-flight join may fill agent_id and be stopped between ticks.
            rec['ai_source'] = source
            rec['ai_ref'] = {key: source[key] for key in ('session_id', 'agent_id', 'channel', 'created_at')}

    def _cancel_jobs(self, rec):
        for key in ('announcement_job', 'history_job', 'confirmation_job'):
            task = rec.get(key)
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
        rec['cursor'] = None

    def _end(self, rec, reason):
        if rec['state'] == 'ended':
            return
        rec.update(state='ended', reason=reason, ended_at=self.clock(), grant=None, rtc=None,
                   needs_device_ack=True)
        self._cancel_jobs(rec)
        self._emit('ended_' + reason if reason in END_REASONS else 'ended_error')

    def _sweep(self):
        now = self.clock()
        rec = self.current
        if rec and self._live(rec):
            if now - rec['caller_seen'] >= 15:
                self._end(rec, 'caller_timeout')
            elif rec['state'] in ('connecting', 'active') and now - rec['device_seen'] >= (40 if rec['state'] == 'connecting' else 15):
                self._end(rec, 'device_offline')
            elif rec['state'] in ('ringing', 'accepting') and (self.last_poll is None or now - self.last_poll >= 40):
                self._end(rec, 'device_offline')
            elif rec['state'] == 'ringing' and now >= rec['ring_until']:
                self._end(rec, 'ringing_timeout')
            elif rec['state'] in ('accepting', 'connecting') and now >= rec['phase_until']:
                self._end(rec, 'connecting_timeout')
            elif rec['state'] == 'active' and now >= rec['expires_at']:
                self._end(rec, 'call_expired')
        expired = [key for key, row in self.records.items()
                   if row['state'] == 'ended' and now - row['ended_at'] >= 60 and not row['needs_device_ack']]
        for key in expired:
            row = self.records.pop(key)
            if self.current is row:
                self.current = None
        self.requests = {key: row for key, row in self.requests.items() if row[1] > now}

    async def _status(self):
        self._sweep()
        return {'device_online': self._online(), 'busy': self._busy()}

    async def _create(self, request_id, caller_name, caller_auth=None, caller_session=None):
        self._sweep()
        if self.closed:
            raise SessionError('service_stopping')
        scope = caller_auth.authorize(caller_session) if caller_auth is not None else 'local'
        request_key = (scope, request_id)
        previous = self.requests.get(request_key)
        if previous:
            rec = self.records.get(previous[0])
            if not rec:
                raise SessionError('request_already_ended', 409)
            return dict(self._caller_response(rec), call_id=rec['id'], access_token=rec['capability'])
        if not self._online():
            raise SessionError('device_offline', 409)
        if self._busy():
            raise SessionError('device_busy', 409)
        if len(self.records) >= 64:
            raise SessionError('too_many_calls', 429)
        if caller_auth is not None:
            caller_auth.admit_create(caller_session)
        now = self.clock()
        rec = {'id': secrets.token_hex(16), 'caller': caller_name, 'capability': secrets.token_urlsafe(32),
               'request_id': request_id, 'state': 'ringing', 'reason': '', 'created_at': now,
               'ring_until': now + 60, 'phase_until': 0, 'expires_at': 0,
               'caller_seen': now, 'device_seen': now, 'ended_at': None,
               'grant': None, 'rtc': None, 'ai_ref': None, 'ai_source': None, 'confirmed': False,
               'issued_human': False, 'needs_device_ack': False,
               'announced_session': None, 'cursor': None, 'history_after': 0,
               'confirmation_after': 0, 'announcement_after': 0,
               'announcement_job': None, 'history_job': None, 'confirmation_job': None}
        self._capture_ai(rec)
        self.current = rec
        self.records[rec['id']] = rec
        self.requests[request_key] = (rec['id'], now + 720)
        self._emit('ringing')
        return dict(self._caller_response(rec), call_id=rec['id'], access_token=rec['capability'])

    def _authorize(self, call_id, capability):
        if not identifier(call_id) or not isinstance(capability, str) or len(capability) > 128:
            raise SessionError('forbidden', 403)
        rec = self.records.get(call_id)
        if (not rec or not hmac.compare_digest(capability.encode(), rec['capability'].encode())
                or (rec['state'] == 'ended' and self.clock() - rec['ended_at'] >= 60)):
            raise SessionError('forbidden', 403)
        return rec

    async def _caller_poll(self, call_id, capability):
        self._sweep()
        rec = self._authorize(call_id, capability)
        rec['caller_seen'] = self.clock()
        return self._caller_response(rec)

    async def _caller_end(self, call_id, capability):
        self._sweep()
        rec = self._authorize(call_id, capability)
        self._end(rec, 'caller_cancel' if rec['state'] == 'ringing' else 'caller_hangup')
        return self._caller_response(rec)

    async def _device_poll(self, value):
        self._sweep()
        self.last_poll, self.device = self.clock(), value
        rec = self.current
        if (rec and rec['state'] == 'ended' and rec['needs_device_ack'] and value['call_id'] == ''
                and (not rec['issued_human'] or (
                    value['state'] in ('idle', 'transition') and not value['media_connected']
                    and value['ai_session_id'] == ''))):
            rec['needs_device_ack'] = False
        if rec and self._live(rec):
            if value['call_id'] == rec['id']:
                rec['device_seen'] = self.clock()
            if rec['state'] in ('ringing', 'accepting'):
                self._capture_ai(rec)
        return self._device_response(rec)

    def _decide(self, rec, action):
        if not self._live(rec) or rec['state'] != 'ringing':
            return
        if action == 'reject':
            self._end(rec, 'rejected')
        else:
            self._capture_ai(rec)
            rec.update(state='accepting', phase_until=self.clock() + 45)
            self._cancel_jobs(rec)

    async def _device_action(self, call_id, action):
        self._sweep()
        rec = self.records.get(call_id)
        if not rec or self.current is not rec:
            raise SessionError('call_not_current', 409)
        if rec['state'] == 'ended':
            return self._device_response(rec)
        if action in ('accept', 'reject'):
            self._decide(rec, action)
        elif action == 'end':
            self._end(rec, 'device_hangup')
        elif action == 'ready':
            if rec['state'] in ('connecting', 'active'):
                return self._device_response(rec, rec['grant'])
            if rec['state'] != 'accepting':
                raise SessionError('call_not_accepted', 409)
            if self.ai._record is not None or not rec['confirmed']:
                self._schedule_confirmation(rec)
                raise SessionError('cleanup_pending', 409)
            now = self.clock()
            channel, expiry = 'stackchan-human-' + secrets.token_hex(16), int(now) + 600
            try:
                token = self.device_token_factory(channel, expiry)
                rtc = self.browser_token_factory(channel, expiry)
            except Exception:
                self._end(rec, 'credential_failed')
                raise SessionError('credential_failed') from None
            rec.update(state='connecting', device_seen=now, phase_until=now + 45, expires_at=expiry, issued_human=True,
                       grant={'session_id': rec['id'], 'server_time': int(now), 'expires_at': expiry,
                              'whip_url': 'https://ap-webrtc-whip.ap.sd-rtn.com/pub/' + channel + '?stringuid=20001&duplex=true',
                              'whip_token': token}, rtc=rtc)
            self._emit('connecting')
            return self._device_response(rec, rec['grant'])
        elif action == 'connected':
            if rec['state'] not in ('connecting', 'active'):
                raise SessionError('call_not_connecting', 409)
            first = rec['state'] != 'active'
            rec.update(state='active', device_seen=self.clock())
            if first:
                self._emit('active')
        return self._device_response(rec)

    @staticmethod
    def _running(task):
        return task is not None and not task.done()

    def _ring_session(self, rec):
        source = self.ai._record
        if (not self._live(rec) or rec['state'] != 'ringing' or not source
                or source['state'] != 'active' or source['cancel'] or not self.device
                or not self._online() or self.device['call_id'] != rec['id']
                or self.device['state'] != 'ringing'
                or not self.device['media_connected']
                or self.device['ai_session_id'] != source['session_id']):
            return None
        return {key: source[key] for key in ('session_id', 'agent_id', 'channel', 'created_at')}

    async def _announce(self, rec, reference):
        try:
            baseline = await asyncio.wait_for(self.provider.history(reference['agent_id'], reference['channel']), 5)
            if self._ring_session(rec) != reference:
                return
            since_ms = int(self.clock() * 1000)
            cursor = HistoryCursor(baseline, since_ms, reference['created_at'] * 1000)
            # Do not read the trigger commands aloud: an acoustic echo of this
            # announcement must never itself be an exact Answer/Decline command.
            await asyncio.wait_for(self.provider.speak(reference['agent_id'], reference['channel'],
                rec['caller'] + ' is calling. Would you like to take the call?'), 5)
            if self._ring_session(rec) == reference:
                rec.update(cursor=cursor, announced_session=reference['session_id'], ai_ref=reference)
                self._emit('announcement_sent')
        except asyncio.CancelledError:
            raise
        except Exception:
            # Screen controls remain available; no unsafe fallback decision.
            if self._live(rec) and rec['state'] == 'ringing':
                self._emit('announcement_retry_failed')
        finally:
            rec['announcement_after'] = self.clock() + 3

    async def _read_commands(self, rec, reference):
        cursor = rec['cursor']
        try:
            history = await asyncio.wait_for(self.provider.history(reference['agent_id'], reference['channel']), 5)
            if self._ring_session(rec) != reference or rec['cursor'] is not cursor:
                return
            commands = cursor.commands(history)
            # Conflicting commands in one newly observed batch are ambiguous.
            if commands and len(set(commands)) == 1:
                self._emit('voice_' + commands[0])
                self._decide(rec, commands[0])
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._live(rec) and rec['state'] == 'ringing':
                self._emit('history_retry_failed')
        finally:
            rec['history_after'] = self.clock() + 1

    def _schedule_confirmation(self, rec):
        if (rec['state'] != 'accepting' or self.ai._record is not None or rec['confirmed']
                or self._running(rec['confirmation_job']) or self.clock() < rec['confirmation_after']):
            return
        rec['confirmation_job'] = asyncio.create_task(self._confirm(rec))

    async def _confirm(self, rec):
        try:
            reference = rec['ai_source']
            # No AI was ever present is different from an unknown join result.
            # A pending join mutates this retained record before Manager drops it.
            clean = reference is None
            if reference is not None and reference['agent_id']:
                clean = await asyncio.wait_for(self.provider.confirm_stopped(reference['agent_id'], reference['channel']), 5)
            if self._live(rec) and rec['state'] == 'accepting' and self.ai._record is None and clean is True:
                rec['confirmed'] = True
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            rec['confirmation_after'] = self.clock() + 1

    async def _tick(self):
        self._sweep()
        rec = self.current
        if not rec or not self._live(rec):
            return
        if rec['state'] == 'accepting':
            self._capture_ai(rec)
            self._schedule_confirmation(rec)
        elif rec['state'] == 'ringing':
            reference = self._ring_session(rec)
            if not reference:
                return
            if rec['announced_session'] != reference['session_id']:
                if not self._running(rec['announcement_job']) and self.clock() >= rec['announcement_after']:
                    rec['announcement_job'] = asyncio.create_task(self._announce(rec, reference))
            elif not self._running(rec['history_job']) and self.clock() >= rec['history_after']:
                rec['history_job'] = asyncio.create_task(self._read_commands(rec, reference))

    async def _watch(self):
        while True:
            await asyncio.sleep(self.watch_interval)
            await self._tick()

    async def _shutdown(self):
        if self.closed:
            return
        if self.current:
            self._end(self.current, 'service_shutdown')
        self.closed = True
        self.watchdog.cancel()
        tasks = [self.watchdog]
        for rec in self.records.values():
            self._cancel_jobs(rec)
            tasks.extend(task for key in ('announcement_job', 'history_job', 'confirmation_job')
                         if (task := rec.get(key)) is not None)
        await asyncio.gather(*tasks, return_exceptions=True)
        self.ai.start_guard = None


def caller_handler_class(manager, web_directory, *, host=None, public_origin=None, auth=None, client_ip=None):
    """Local by default; public callers require an invite and exact HTTPS origin.

    host is the actual allowed HTTP Host, independent of the socket bind address.
    client_ip may extract an IP only from a trusted, authenticated reverse proxy;
    without that callback, forwarded headers are ignored and the peer IP is used.
    """
    directory = Path(web_directory)
    if (public_origin is None) != (auth is None):
        raise ValueError('public_origin_and_auth_must_be_configured_together')
    if public_origin is not None:
        parsed = urlsplit(public_origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or parsed.netloc != parsed.netloc.lower()):
            raise ValueError('public_origin_must_be_an_exact_https_origin')
        expected_host = host or parsed.netloc
        expected_origin = public_origin
    else:
        expected_host = host or '127.0.0.1:8769'
        expected_origin = 'http://' + expected_host

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _allowed(self):
            if (auth is not None and self.command == 'GET' and self.path == '/'
                    and self.headers.get('Host') == expected_host and self.headers.get('Origin') is None
                    and self.headers.get('Sec-Fetch-Mode') == 'navigate'
                    and self.headers.get('Sec-Fetch-Dest') == 'document'):
                return True  # A friend may follow the invite from another website.
            origins = (expected_origin,) if auth is not None and self.command == 'POST' else (None, expected_origin)
            return (self.headers.get('Host') == expected_host
                    and self.headers.get('Origin') in origins
                    and self.headers.get('Sec-Fetch-Site') in (None, 'none', 'same-origin'))

        def _bearer(self):
            value = self.headers.get('Authorization', '')
            return value[7:] if value.startswith('Bearer ') else ''

        def reply(self, status, value, content_type='application/json'):
            data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Permissions-Policy', 'microphone=(self), camera=()')
            if status == 429:
                self.send_header('Retry-After', '60')
            self.send_header('Connection', 'close')
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self._allowed():
                self.reply(403, {'error': 'forbidden'}); return
            try:
                if self.path == '/status':
                    if auth is not None:
                        auth.authorize(self._bearer())
                    self.reply(200, manager.status())
                elif self.path == '/auth/config':
                    self.reply(200, {'required': auth is not None})
                elif self.path in ('/', '/agora.js'):
                    filename = 'caller.html' if self.path == '/' else 'agora.js'
                    self.reply(200, (directory / filename).read_bytes(),
                               'text/html; charset=utf-8' if filename.endswith('.html') else 'text/javascript')
                else:
                    self.reply(404, {'error': 'not_found'})
            except SessionError as exc:
                self.reply(exc.status, {'error': exc.code})
            except Exception:
                self.reply(503, {'error': 'service_unavailable'})

        def do_POST(self):
            if not self._allowed():
                self.reply(403, {'error': 'forbidden'}); return
            if self.path not in ('/calls', '/calls/poll', '/calls/end', '/auth/session'):
                self.reply(404, {'error': 'not_found'}); return
            if self.path == '/auth/session' and auth is None:
                self.reply(404, {'error': 'not_found'}); return
            if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding'):
                self.reply(415, {'error': 'json_required'}); return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 512:
                    raise ValueError()
                self.connection.settimeout(3)
                raw = self.rfile.read(size)
                value = json.loads(raw)
                keys = {'invite'} if self.path == '/auth/session' else {'request_id', 'caller_name'} if self.path == '/calls' else {'call_id'}
                if len(raw) != size or not isinstance(value, dict) or set(value) != keys:
                    raise ValueError()
            except (ValueError, TimeoutError, ConnectionError):
                self.reply(400, {'error': 'invalid_request'}); return
            try:
                if self.path == '/auth/session':
                    result = auth.exchange(value['invite'], client_ip(self) if client_ip is not None else self.client_address[0])
                elif self.path == '/calls':
                    if auth is None:
                        result = manager.create(value['request_id'], value['caller_name'])
                    else:
                        # Authorization is also repeated by the single state owner
                        # before idempotency lookup and new-call rate accounting.
                        auth.authorize(self._bearer())
                        result = manager.create(value['request_id'], value['caller_name'],
                                                caller_auth=auth, caller_session=self._bearer())
                else:
                    operation = manager.caller_poll if self.path == '/calls/poll' else manager.caller_end
                    result = operation(value['call_id'], self._bearer())
                self.reply(200, result)
            except SessionError as exc:
                self.reply(exc.status, {'error': exc.code})
            except Exception:
                self.reply(503, {'error': 'service_unavailable'})
    return Handler
