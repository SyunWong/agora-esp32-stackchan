"""Shared-password protocol and durable-session checks; synthetic secrets only."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_state import SQLite, Provider
from api import CloudAPI
from caller_sessions import CloudCallerAuth, CloudPasswordAuth
from stackchan_ai import SessionError
from state import CloudCallManager, CloudManager, SqlJournal

PASSWORD, REPLACEMENT = 'test-pass', 'next-pass'
ORIGIN, DEVICE_KEY = 'https://calls.example.test', 'd' * 43
IP = 'test-client'


class FaultSQL:
    def __init__(self, sql):
        self.sql, self.fail_write, self.fail_read = sql, False, False
        self.before_write, self.writes = None, 0

    def exec(self, statement, *values):
        if statement.startswith('SELECT value FROM caller_password_auth_state') and self.fail_read:
            raise RuntimeError('synthetic-private-read-detail')
        if statement.startswith('INSERT INTO caller_password_auth_state'):
            if self.before_write:
                self.before_write()
            if self.fail_write:
                raise RuntimeError('synthetic-private-write-detail')
            self.writes += 1
        return self.sql.exec(statement, *values)


class PasswordAuthTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000
        self.sql = SQLite()
        self.storage = FaultSQL(self.sql)
        self.auth = self.owner()

    def tearDown(self):
        self.sql.connection.close()

    def owner(self, password=PASSWORD):
        return CloudPasswordAuth(password, self.storage, clock=lambda: self.now)

    def error(self, code, operation):
        with self.assertRaises(SessionError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def stored(self):
        return self.sql.exec('SELECT value FROM caller_password_auth_state WHERE id=1').toArray()[0].value

    def test_eight_hour_ttl_survives_reconstruction_without_sliding_extension(self):
        grant = self.auth.exchange(PASSWORD, IP)
        self.assertEqual(grant['expires_at'], self.now + 8 * 3600)
        self.assertTrue(self.auth._token(grant['access_token']))
        self.now += 8 * 3600 - 1
        self.assertEqual(self.owner().authorize(grant['access_token']), self.auth.invite_id)
        self.now += 1
        self.error('session_expired', lambda: self.owner().authorize(grant['access_token']))

    def test_password_has_no_thirty_day_expiration_and_is_case_sensitive(self):
        self.now += 31 * 86400
        self.assertTrue(self.owner().exchange(PASSWORD, IP)['access_token'])
        for value in (PASSWORD.upper(), ' ' + PASSWORD, PASSWORD + ' '):
            self.error('password_invalid', lambda: self.auth.exchange(value, IP))

    def test_password_rotation_revokes_sessions_without_resurrection_or_rate_reset(self):
        old = self.auth.exchange(PASSWORD, IP)['access_token']
        rotated = self.owner(REPLACEMENT)
        self.error('session_expired', lambda: rotated.authorize(old))
        self.error('password_invalid', lambda: rotated.exchange(PASSWORD, IP))
        new = rotated.exchange(REPLACEMENT, IP)['access_token']
        restored = self.owner()
        self.error('session_expired', lambda: restored.authorize(old))
        self.error('session_expired', lambda: restored.authorize(new))
        restored.exchange(PASSWORD, IP)
        restored.exchange(PASSWORD, IP)
        self.error('rate_limited', lambda: self.owner().exchange(PASSWORD, IP))

    def test_failed_guesses_include_invalid_values_and_survive_each_rebuild(self):
        for value in ('wrong', '', None, False, ['wrong']):
            self.error('password_invalid', lambda: self.auth.exchange(value, IP))
            self.auth = self.owner()
        writes = self.storage.writes
        for _ in range(3):
            self.error('rate_limited', lambda: self.auth.exchange(PASSWORD, IP))
        self.assertEqual(self.storage.writes, writes)
        self.now += 60
        self.assertTrue(self.owner().exchange(PASSWORD, IP))

    def test_global_guess_and_create_limits_survive_rebuild(self):
        for index in range(self.auth.EXCHANGES_PER_MINUTE):
            self.error('password_invalid', lambda: self.auth.exchange('wrong', f'client-{index}'))
        self.error('rate_limited', lambda: self.owner().exchange(PASSWORD, 'new-client'))
        self.now += 60
        tokens = [self.owner().exchange(PASSWORD, f'client-{index}')['access_token'] for index in range(5)]
        for token in tokens[:4]:
            for _ in range(3):
                self.owner().admit_create(token)
            self.error('rate_limited', lambda: self.owner().admit_create(token))
        self.error('rate_limited', lambda: self.owner().admit_create(tokens[4]))

    def test_only_digests_and_limits_are_persisted_and_bearer_format_is_unchanged(self):
        token = self.auth.exchange(PASSWORD, IP)['access_token']
        raw = self.stored()
        for secret in (PASSWORD, token, IP, self.auth._password_credential(PASSWORD)):
            self.assertNotIn(secret, raw)
        self.assertEqual(set(json.loads(raw)['sessions']), {self.auth._digest(token).hex()})
        self.error('session_required', lambda: self.auth.authorize('TEST'))
        self.error('session_expired', lambda: self.auth.authorize(self.auth._password_credential(PASSWORD)))

    def test_commit_precedes_publication_and_write_failure_issues_no_session(self):
        generated = 'n' * 43
        self.storage.before_write = lambda: self.assertEqual(self.auth._sessions, {})
        self.storage.fail_write = True
        with patch('caller_auth.secrets.token_urlsafe', return_value=generated):
            self.error('service_unavailable', lambda: self.auth.exchange(PASSWORD, IP))
        self.assertEqual(self.auth._sessions, {})
        self.error('service_unavailable', lambda: self.auth.authorize(generated))
        self.storage.fail_write = False
        self.error('session_expired', lambda: self.owner().authorize(generated))

    def test_invalid_journal_and_failed_rotation_stay_closed(self):
        token = self.auth.exchange(PASSWORD, IP)['access_token']
        valid = self.stored()
        row = json.loads(valid)
        row['sessions'][self.auth._digest(token).hex()]['expires_at'] = self.now + 8 * 3600 + 1
        for raw in ('null', '{"version":1,"version":1}', json.dumps(row)):
            self.sql.exec('UPDATE caller_password_auth_state SET value=? WHERE id=1', raw)
            broken = self.owner()
            self.error('service_unavailable', lambda: broken.exchange(PASSWORD, IP))
            self.error('service_unavailable', lambda: broken.authorize(token))
        self.sql.exec('UPDATE caller_password_auth_state SET value=? WHERE id=1', valid)
        self.storage.fail_write = True
        rotated = self.owner(REPLACEMENT)
        self.error('service_unavailable', lambda: rotated.exchange(REPLACEMENT, IP))

    def test_legacy_invitation_table_remains_readable_for_code_rollback(self):
        invite = 'i' * 43
        legacy = CloudCallerAuth(invite, self.sql, invite_expires_at=self.now + 86400, clock=lambda: self.now)
        legacy_token = legacy.exchange(invite, IP)['access_token']
        before = self.sql.exec('SELECT value FROM caller_auth_state WHERE id=1').toArray()[0].value
        password_token = self.auth.exchange(PASSWORD, IP)['access_token']
        self.error('session_expired', lambda: self.auth.authorize(legacy_token))
        restored = CloudCallerAuth(invite, self.sql, invite_expires_at=self.now + 86400, clock=lambda: self.now)
        self.assertEqual(restored.authorize(legacy_token), legacy.invite_id)
        self.error('session_expired', lambda: restored.authorize(password_token))
        self.assertEqual(self.sql.exec('SELECT value FROM caller_auth_state WHERE id=1').toArray()[0].value, before)

    def test_password_input_bounds_do_not_relax_token_validation(self):
        for value in (None, '', False, 'x' * 129, '\u4e00' * 86, '\ud800'):
            with self.assertRaises(ValueError):
                self.owner(value)
        for index, value in enumerate(('x' * 129, '\u4e00' * 86, '\ud800')):
            self.error('password_invalid', lambda: self.auth.exchange(value, f'client-{index}'))
        short = self.owner('TEST')
        self.assertTrue(short.exchange('TEST', 'short-client'))


class PasswordAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql, self.provider = SQLite(), Provider()
        self.storage = FaultSQL(self.sql)
        self.auth = CloudPasswordAuth(PASSWORD, self.storage)
        self.ai = CloudManager(self.provider, lambda *_: 'synthetic-device-token', SqlJournal(self.sql))
        self.calls = CloudCallManager(self.ai, lambda *_: 'synthetic-device-token', lambda *_: {})
        self.api = CloudAPI(self.ai, self.calls, self.auth, ORIGIN, DEVICE_KEY)

    async def asyncTearDown(self):
        tasks = self.calls.tasks()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.sql.connection.close()

    async def send(self, path, body=None, token=None, *, method='POST', device=False, origin=ORIGIN):
        headers = {'host': 'calls.example.test', 'content-type': 'application/json'}
        if device:
            headers['authorization'] = 'Bearer ' + DEVICE_KEY
        else:
            headers['origin'] = origin
            if token:
                headers['authorization'] = 'Bearer ' + token
        return await self.api.handle(method, path, headers, json.dumps(body).encode(), IP)

    async def error(self, code, *args, **kwargs):
        with self.assertRaises(SessionError) as caught:
            await self.send(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    async def test_public_mode_and_strict_password_exchange_shape(self):
        self.assertEqual(await self.send('/auth/config', method='GET'), {'required': True, 'mode': 'password'})
        for body in ({'invite': PASSWORD}, {'password': PASSWORD, 'invite': 'unused'}, {}, []):
            await self.error('invalid_request', '/auth/session', body)
        self.assertEqual((await self.error('password_invalid', '/auth/session', {'password': 'wrong'})).status, 401)
        token = (await self.send('/auth/session', {'password': PASSWORD}))['access_token']
        self.assertEqual(await self.send('/status', token=token, method='GET'), {'device_online': False, 'busy': False})
        await self.error('forbidden', '/auth/session', {'password': PASSWORD}, origin='https://other.example')

    async def test_password_or_legacy_invite_cannot_be_used_as_session_bearer(self):
        for value in (PASSWORD, 'i' * 43):
            error = await self.error('session_required' if value == PASSWORD else 'session_expired',
                '/status', token=value, method='GET')
            self.assertEqual(error.status, 401)

    async def test_storage_failure_blocks_new_auth_not_device_or_existing_call_capability(self):
        idle = {'state': 'idle', 'call_id': '', 'ai_session_id': '', 'media_connected': False}
        await self.send('/device/poll', idle, device=True)
        token = (await self.send('/auth/session', {'password': PASSWORD}))['access_token']
        call = await self.send('/calls', {'request_id': 'a' * 32, 'caller_name': 'Test'}, token)
        self.storage.fail_write = True
        await self.error('service_unavailable', '/auth/session', {'password': PASSWORD})
        self.assertEqual((await self.send('/device/poll', idle, device=True))['call']['state'], 'ringing')
        for path in ('/calls/poll', '/calls/end'):
            await self.send(path, {'call_id': call['call_id']}, call['access_token'])
        self.assertEqual(self.calls.current['state'], 'ended')
        self.assertEqual(self.provider.starts, [])

    async def test_reconstruction_keeps_password_session_but_not_old_call_or_media(self):
        idle = {'state': 'idle', 'call_id': '', 'ai_session_id': '', 'media_connected': False}
        await self.send('/device/poll', idle, device=True)
        token = (await self.send('/auth/session', {'password': PASSWORD}))['access_token']
        call = await self.send('/calls', {'request_id': 'a' * 32, 'caller_name': 'Test'}, token)
        restored_auth = CloudPasswordAuth(PASSWORD, self.sql)
        restored_calls = CloudCallManager(self.ai, lambda *_: 'synthetic-token', lambda *_: {})
        self.assertEqual(restored_auth.authorize(token), self.auth.invite_id)
        with self.assertRaises(SessionError) as caught:
            await restored_calls.caller_poll(call['call_id'], call['access_token'])
        self.assertEqual(caught.exception.code, 'forbidden')
        response = await restored_calls.device_poll({'state': 'human', 'call_id': call['call_id'],
            'ai_session_id': '', 'media_connected': True})
        self.assertIsNone(response['call'])
        self.assertFalse(restored_calls.recovered)
        raw = self.sql.exec('SELECT value FROM caller_password_auth_state WHERE id=1').toArray()[0].value
        self.assertNotIn(call['call_id'], raw)
        self.assertNotIn(call['access_token'], raw)


if __name__ == '__main__':
    unittest.main()
