"""Cloud HTTP protocol checks with real managers and fake cloud/media providers."""
import asyncio
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'service'), str(ROOT / 'cloud'), str(Path(__file__).parent)]
from api import CloudAPI, DEVICE_PATHS
from caller_auth import CallerAuth
from stackchan_ai import SessionError
from state import CloudCallManager, CloudManager, SqlJournal, finish_tasks
from test_state import Provider, SQLite

ORIGIN = 'https://calls.example.test'
DEVICE_KEY = 'd' * 43
INVITE = 'i' * 43


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 1800000000
        self.provider, self.sql = Provider(), SQLite()
        self.grants = []
        self.ai = CloudManager(self.provider, lambda *args: 'ai-test-token',
                              SqlJournal(self.sql), clock=lambda: self.now)
        self.calls = CloudCallManager(self.ai, self.device_token, self.browser_token,
                                     clock=lambda: self.now)
        self.auth = CallerAuth(INVITE, invite_expires_at=self.now + 86400,
                               clock=lambda: self.now, lock_factory=nullcontext)
        self.api = CloudAPI(self.ai, self.calls, self.auth, ORIGIN, DEVICE_KEY)

    async def asyncTearDown(self):
        tasks = self.calls.tasks()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.sql.connection.close()

    def device_token(self, channel, expiry):
        self.grants.append(channel)
        return 'human-test-token'

    @staticmethod
    def browser_token(channel, expiry):
        return {'appId': 'test-app', 'channel': channel, 'uid': '20002',
                'robotUid': '20001', 'token': 'browser-test-token',
                'expiresAt': expiry, 'ttl': 600}

    @staticmethod
    def headers(*, device=False, token=None):
        result = {'host': 'calls.example.test', 'content-type': 'application/json'}
        if device:
            result['authorization'] = 'Bearer ' + DEVICE_KEY
        else:
            result.update(origin=ORIGIN, **{'sec-fetch-site': 'same-origin'})
        if token is not None:
            result['authorization'] = 'Bearer ' + token
        return result

    async def send(self, path, value=None, *, method='POST', device=False, token=None, headers=None, raw=None):
        headers = self.headers(device=device, token=token) if headers is None else headers
        raw = json.dumps(value, separators=(',', ':')).encode() if raw is None else raw
        return await self.api.handle(method, path, headers, raw, client_ip='test-client')

    async def error(self, status, code, path, value=None, **kwargs):
        with self.assertRaises(SessionError) as caught:
            await self.send(path, value, **kwargs)
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    async def online(self, **updates):
        value = {'state': 'idle', 'ai_session_id': '', 'call_id': '', 'media_connected': False}
        value.update(updates)
        return await self.send('/device/poll', value, device=True)

    async def guest(self):
        response = await self.send('/auth/session', {'invite': INVITE})
        self.assertEqual(set(response), {'access_token', 'expires_at'})
        self.assertEqual(response['expires_at'], self.now + 900)
        return response['access_token']

    async def call(self, token, request='1' * 32, name='Alex'):
        return await self.send('/calls', {'request_id': request, 'caller_name': name}, token=token)

    async def action(self, call, action):
        return await self.send('/device/call', {'call_id': call['call_id'], 'action': action}, device=True)

    async def tick(self):
        await self.calls.tick()
        await finish_tasks(self.calls.tasks())

    async def test_public_config_and_private_presence_match_caller_contract(self):
        self.assertEqual(await self.send('/auth/config', method='GET'), {'required': True})
        await self.error(401, 'session_required', '/status', method='GET')
        token = await self.guest()
        self.assertEqual(await self.send('/status', method='GET', token=token),
                         {'device_online': False, 'busy': False})
        self.assertEqual(set(await self.online()), {'server_time', 'call'})
        self.assertEqual(await self.send('/status', method='GET', token=token),
                         {'device_online': True, 'busy': False})

    async def test_exact_host_on_every_api_family(self):
        cases = [('/auth/config', 'GET', False, None), ('/status', 'GET', False, None),
                 ('/auth/session', 'POST', False, {'invite': INVITE}),
                 ('/device/poll', 'POST', True, {})]
        for host in ('evil.example', 'calls.example.test.evil', 'calls.example.test:443', ''):
            for path, method, device, value in cases:
                with self.subTest(host=host, path=path):
                    headers = dict(self.headers(device=device), host=host)
                    await self.error(403, 'forbidden', path, value, method=method, headers=headers)

    async def test_device_bearer_never_accepts_browser_origin_or_fetch_metadata(self):
        body = {'request_id': 'request_ai_0001'}
        for patch in ({'origin': ORIGIN}, {'origin': 'null'},
                      {'sec-fetch-site': 'same-origin'}, {'sec-fetch-site': 'none'},
                      {'authorization': ''}, {'authorization': 'Bearer wrong'},
                      {'authorization': 'Bearer 非ASCII'}):
            await self.error(403, 'forbidden', '/ai/start', body,
                             headers=dict(self.headers(device=True), **patch))
        await self.error(403, 'forbidden', '/ai/start', method='GET', device=True)
        self.assertEqual(self.provider.starts, [])

    async def test_caller_mutations_require_exact_origin_and_same_site(self):
        for patch in ({'origin': None}, {'origin': 'null'}, {'origin': 'https://evil.example'},
                      {'origin': ORIGIN + '/'}, {'sec-fetch-site': 'cross-site'},
                      {'sec-fetch-site': 'same-site'}):
            headers = dict(self.headers(), **patch)
            await self.error(403, 'forbidden', '/auth/session', {'invite': INVITE}, headers=headers)
        # Non-browser GETs may omit Origin; status still requires session auth.
        bare = {'host': 'calls.example.test'}
        self.assertEqual(await self.send('/auth/config', method='GET', headers=bare), {'required': True})

    async def test_paths_are_exact_and_methods_do_not_mutate(self):
        for path in ('/', '/agora.js', '/transcript', '/session', '/ai/start?x=1',
                     '/calls/', '/calls/poll/../end', '/admin'):
            await self.error(404, 'not_found', path, {}, headers=self.headers(device=True))
        await self.error(404, 'not_found', '/calls', method='GET')
        await self.error(405, 'method_not_allowed', '/calls', {}, method='PUT')
        await self.error(400, 'invalid_request', '/status', {})
        self.assertEqual(self.calls.records, {})

    async def test_json_content_type_transfer_encoding_and_byte_limits(self):
        for content_type in ('text/plain', 'application/x-www-form-urlencoded', ''):
            await self.error(415, 'json_required', '/auth/session', {'invite': INVITE},
                             headers=dict(self.headers(), **{'content-type': content_type}))
        await self.error(415, 'json_required', '/auth/session', {'invite': INVITE},
                         headers=dict(self.headers(), **{'transfer-encoding': 'chunked'}))
        for raw in (b'', b'x' * 513, b'{', b'\xff', b'[]', b'null', b'42'):
            await self.error(400, 'invalid_request', '/auth/session', raw=raw)
        await self.error(400, 'invalid_request', '/ai/start', device=True, raw=b' ' * 257)
        good = dict(self.headers(), **{'content-type': 'application/json; charset=utf-8'})
        self.assertIn('access_token', await self.send('/auth/session', {'invite': INVITE}, headers=good))

    async def test_exact_object_shapes_on_all_mutations(self):
        bodies = {'/auth/session': {'invite': INVITE},
            '/calls': {'request_id': '1' * 32, 'caller_name': 'Alex'},
            '/calls/poll': {'call_id': '1' * 32}, '/calls/end': {'call_id': '1' * 32},
            '/device/poll': {'state': 'idle', 'ai_session_id': '', 'call_id': '', 'media_connected': False},
            '/device/call': {'call_id': '1' * 32, 'action': 'accept'},
            '/ai/start': {'request_id': 'request_ai_0001'}, '/ai/heartbeat': {'session_id': 'a' * 32},
            '/ai/stop': {'session_id': 'a' * 32}}
        for path, value in bodies.items():
            for malformed in ({}, dict(value, unexpected=True)):
                with self.subTest(path=path, body=malformed):
                    await self.error(400, 'invalid_request', path, malformed, device=path in DEVICE_PATHS)
        await self.error(400, 'invalid_request', '/ai/stop',
                         {'request_id': 'request_ai_0001', 'session_id': 'a' * 32}, device=True)

    async def test_device_non_poll_fields_must_be_strings_as_in_local_http(self):
        for path, value in (('/ai/start', {'request_id': None}),
                            ('/ai/heartbeat', {'session_id': 7}),
                            ('/ai/stop', {'session_id': None}),
                            ('/ai/stop', {'request_id': []}),
                            ('/device/call', {'call_id': '1' * 32, 'action': {}})):
            with self.subTest(path=path, value=value):
                await self.error(400, 'invalid_request', path, value, device=True)

    async def test_device_poll_rejects_invalid_types_before_marking_online(self):
        value = {'state': 'idle', 'ai_session_id': '', 'call_id': '', 'media_connected': False}
        for patch in ({'media_connected': 1}, {'call_id': None}, {'ai_session_id': []},
                      {'state': 'unknown'}, {'state': []}):
            await self.error(400, 'invalid_request', '/device/poll', dict(value, **patch), device=True)
        self.assertFalse((await self.calls.status())['device_online'])

    async def test_invite_and_session_cannot_substitute_device_or_call_credentials(self):
        await self.online()
        token = await self.guest()
        call = await self.call(token)
        for wrong in (INVITE, token, call['access_token']):
            await self.error(403, 'forbidden', '/ai/start', {'request_id': 'request_ai_0001'},
                             headers=self.headers(device=True, token=wrong))
        for wrong in (INVITE, DEVICE_KEY, call['access_token']):
            await self.error(401, 'session_expired', '/status', method='GET', token=wrong)
        for wrong in (INVITE, DEVICE_KEY, token):
            await self.error(403, 'forbidden', '/calls/end', {'call_id': call['call_id']}, token=wrong)
        self.assertEqual(self.calls.current['state'], 'ringing')

    async def test_invitation_expiry_failed_guess_rate_and_fixed_error_codes(self):
        for _ in range(5):
            await self.error(401, 'invitation_invalid', '/auth/session', {'invite': 'x' * 43})
        await self.error(429, 'rate_limited', '/auth/session', {'invite': INVITE})
        self.now += 60
        self.auth.invite_expires_at = self.now
        await self.error(401, 'invitation_expired', '/auth/session', {'invite': INVITE})
        self.assertEqual(self.calls.records, {})

    async def test_create_offline_then_idempotent_busy_and_name_constraints(self):
        token = await self.guest()
        await self.error(409, 'device_offline', '/calls',
                         {'request_id': '1' * 32, 'caller_name': 'Alex'}, token=token)
        await self.online()
        for name in ('', 'Alex1', '<b>Alex</b>', 'A' * 33):
            await self.error(400, 'invalid_caller_name', '/calls',
                             {'request_id': '1' * 32, 'caller_name': name}, token=token)
        call = await self.call(token, name="O'Neil")
        self.assertEqual(set(call), {'call_id', 'access_token', 'call', 'rtc'})
        self.assertEqual(set(call['call']), {'id', 'caller', 'state', 'reason'})
        self.assertIsNone(call['rtc'])
        self.assertEqual(await self.call(token, name="O'Neil"), call)
        await self.error(409, 'device_busy', '/calls',
                         {'request_id': '2' * 32, 'caller_name': 'Sam'}, token=token)
        self.assertEqual(await self.send('/status', method='GET', token=token),
                         {'device_online': True, 'busy': True})

    async def test_ai_endpoints_keep_existing_device_protocol_and_tombstones(self):
        await self.online()
        started = await self.send('/ai/start', {'request_id': 'request_ai_0001'}, device=True)
        self.assertEqual(set(started), {'session_id', 'server_time', 'whip_url', 'whip_token',
                                        'expires_at', 'lease_seconds'})
        self.assertEqual(await self.send('/ai/start', {'request_id': 'request_ai_0001'}, device=True), started)
        beat = await self.send('/ai/heartbeat', {'session_id': started['session_id']}, device=True)
        self.assertEqual(beat, {'active': True, 'state': 'active', 'expires_at': self.now + 600, 'lease_seconds': 45})
        stopped = await self.send('/ai/stop', {'request_id': 'request_ai_0001'}, device=True)
        self.assertEqual(stopped, {'active': False, 'state': 'stopped'})
        self.assertEqual(await self.send('/ai/stop', {'session_id': started['session_id']}, device=True), stopped)
        await self.error(409, 'request_already_stopped', '/ai/start', {'request_id': 'request_ai_0001'}, device=True)

    async def test_private_human_grant_waits_for_accept_ai_stop_and_confirmation(self):
        await self.online()
        ai = await self.send('/ai/start', {'request_id': 'request_ai_0001'}, device=True)
        await self.online(state='ai', ai_session_id=ai['session_id'], media_connected=True)
        token = await self.guest()
        call = await self.call(token)
        poll_body = {'call_id': call['call_id']}
        self.assertIsNone((await self.send('/calls/poll', poll_body, token=call['access_token']))['rtc'])
        await self.error(409, 'call_not_accepted', '/device/call',
                         dict(poll_body, action='ready'), device=True)
        accepted = await self.action(call, 'accept')
        self.assertNotIn('grant', accepted)
        self.assertEqual(accepted['call']['state'], 'accepting')
        await self.error(409, 'cleanup_pending', '/device/call', dict(poll_body, action='ready'), device=True)
        self.provider.confirmed = False
        await self.send('/ai/stop', {'session_id': ai['session_id']}, device=True)
        await self.tick()
        await self.error(409, 'cleanup_pending', '/device/call', dict(poll_body, action='ready'), device=True)
        self.assertEqual(self.grants, [])
        self.provider.confirmed = True
        self.now += 1
        await self.tick()
        ready = await self.action(call, 'ready')
        self.assertEqual(set(ready['grant']), {'session_id', 'server_time', 'expires_at', 'whip_url', 'whip_token'})
        self.assertIn('stackchan-human-', ready['grant']['whip_url'])
        caller = await self.send('/calls/poll', poll_body, token=call['access_token'])
        self.assertEqual(caller['rtc']['uid'], '20002')
        self.assertEqual(caller['rtc']['robotUid'], '20001')
        self.assertEqual(caller['rtc']['channel'], self.grants[0])
        self.assertNotEqual(caller['rtc']['channel'], self.provider.starts[0])
        await self.action(call, 'connected')
        await self.error(409, 'human_call_in_progress', '/ai/start', {'request_id': 'request_ai_0002'}, device=True)
        ended = await self.send('/calls/end', poll_body, token=call['access_token'])
        self.assertIsNone(ended['rtc'])
        self.assertEqual(ended['call']['reason'], 'caller_hangup')
        for action in ('ready', 'connected'):
            late = await self.action(call, action)
            self.assertNotIn('grant', late)
            self.assertEqual(late['call']['state'], 'ended')
        await self.online()
        await self.send('/ai/start', {'request_id': 'request_ai_0002'}, device=True)

    async def test_rejection_and_caller_cancel_never_mint_media_credentials(self):
        await self.online()
        token = await self.guest()
        call = await self.call(token)
        result = await self.action(call, 'reject')
        self.assertEqual(result['call']['reason'], 'rejected')
        await self.online()
        second = await self.call(token, '2' * 32)
        result = await self.send('/calls/end', {'call_id': second['call_id']}, token=second['access_token'])
        self.assertEqual(result['call']['reason'], 'caller_cancel')
        self.assertEqual(self.grants, [])
        self.assertEqual(self.provider.starts, [])

    async def test_identity_expiry_does_not_break_existing_capability_poll_or_hangup(self):
        await self.online()
        token = await self.guest()
        call = await self.call(token)
        self.auth._sessions.clear()  # Same effect as session expiry/authorization-instance rotation.
        await self.error(401, 'session_expired', '/status', method='GET', token=token)
        await self.error(401, 'session_expired', '/calls',
                         {'request_id': '2' * 32, 'caller_name': 'Sam'}, token=token)
        body = {'call_id': call['call_id']}
        self.assertEqual((await self.send('/calls/poll', body, token=call['access_token']))['call']['state'], 'ringing')
        self.assertEqual((await self.send('/calls/end', body, token=call['access_token']))['call']['state'], 'ended')


if __name__ == '__main__':
    unittest.main()
