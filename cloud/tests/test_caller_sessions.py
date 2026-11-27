"""Real SQLite recovery tests; no network, real credentials or Worker deployment."""
import asyncio
import json
import unittest
from unittest.mock import patch

from test_state import SQLite, Provider
from api import CloudAPI
from caller_sessions import CloudCallerAuth
from stackchan_ai import SessionError
from state import CloudCallManager, CloudManager, SqlJournal

INVITE, REPLACEMENT = 'i' * 40, 'r' * 40
IP = '192.0.2.1'


class FaultSQL:
    def __init__(self, sql):
        self.sql, self.fail_read, self.fail_write = sql, False, False
        self.before_write = None
        self.writes = 0

    def exec(self, statement, *args):
        if statement.startswith('SELECT value FROM caller_auth_state') and self.fail_read:
            raise RuntimeError('test private read detail')
        if statement.startswith('INSERT INTO caller_auth_state'):
            if self.before_write:
                self.before_write()
            if self.fail_write:
                raise RuntimeError('test private write detail')
            self.writes += 1
        return self.sql.exec(statement, *args)


class CallerSessionTests(unittest.TestCase):
    def setUp(self):
        self.now = 1800000000
        self.expiry = self.now + 30 * 86400
        self.sql = SQLite()
        self.storage = FaultSQL(self.sql)
        self.auth = self.owner()

    def tearDown(self):
        self.sql.connection.close()

    def owner(self, invite=INVITE, expiry=None):
        return CloudCallerAuth(invite, self.storage,
                               invite_expires_at=self.expiry if expiry is None else expiry,
                               clock=lambda: self.now)

    def session(self, ip=IP):
        return self.auth.exchange(INVITE, ip)['access_token']

    def assert_error(self, code, operation):
        with self.assertRaises(SessionError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def stored(self):
        return self.sql.exec('SELECT value FROM caller_auth_state WHERE id=1').toArray()[0].value

    def test_session_recovers_after_idle_reconstruction_and_expires_at_original_ttl(self):
        grant = self.auth.exchange(INVITE, IP)
        self.assertEqual(grant['expires_at'], self.now + 900)
        self.now += 30
        self.auth = self.owner()
        self.assertEqual(self.auth.authorize(grant['access_token']), self.auth.invite_id)
        self.now += 869
        self.assertEqual(self.owner().authorize(grant['access_token']), self.auth.invite_id)
        self.now += 1
        self.assert_error('session_expired', lambda: self.owner().authorize(grant['access_token']))

    def test_storage_contains_only_digests_expirations_and_rate_metadata(self):
        token = self.session()
        self.auth.admit_create(token)
        raw = self.stored()
        for secret in (INVITE, token, IP):
            self.assertNotIn(secret, raw)
        row = json.loads(raw)
        self.assertEqual(set(row['sessions']), {self.auth._digest(token).hex()})
        self.assertEqual(set(row['ips']), {self.auth._digest(IP).hex()})
        self.assertEqual(set(row['sessions'][self.auth._digest(token).hex()]), {'expires_at', 'creates'})

    def test_rotation_revokes_and_switching_back_does_not_resurrect(self):
        token = self.session()
        rotated = self.owner(REPLACEMENT)
        self.assert_error('session_expired', lambda: rotated.authorize(token))
        self.assert_error('invitation_invalid', lambda: rotated.exchange(INVITE, IP))
        new_token = rotated.exchange(REPLACEMENT, IP)['access_token']
        restored = self.owner()
        self.assert_error('session_expired', lambda: restored.authorize(token))
        self.assert_error('session_expired', lambda: restored.authorize(new_token))

    def test_expiry_change_revokes_and_invite_expiry_bounds_new_session(self):
        token = self.session()
        shortened = self.owner(expiry=self.now + 30)
        self.assert_error('session_expired', lambda: shortened.authorize(token))
        grant = shortened.exchange(INVITE, IP)
        self.assertEqual(grant['expires_at'], self.now + 30)
        self.now += 30
        expired = self.owner(expiry=self.now)
        self.assert_error('session_expired', lambda: expired.authorize(grant['access_token']))
        self.assert_error('invitation_expired', lambda: expired.exchange(INVITE, IP))

    def test_failed_guess_and_per_ip_limit_survive_every_reconstruction(self):
        for _ in range(self.auth.EXCHANGES_PER_IP_MINUTE):
            self.assert_error('invitation_invalid', lambda: self.auth.exchange('wrong', IP))
            self.auth = self.owner()
        self.assert_error('rate_limited', lambda: self.session())
        self.assertTrue(self.session('192.0.2.2'))
        self.now += 60
        self.auth = self.owner()
        self.assertTrue(self.session())

    def test_global_exchange_limit_survives_reconstruction_and_rotation(self):
        for index in range(self.auth.EXCHANGES_PER_MINUTE):
            self.assert_error('invitation_invalid', lambda: self.auth.exchange('wrong', f'192.0.2.{index}'))
        self.auth = self.owner()
        self.assert_error('rate_limited', lambda: self.session('198.51.100.1'))
        rotated = self.owner(REPLACEMENT)
        self.assert_error('rate_limited', lambda: rotated.exchange(REPLACEMENT, '198.51.100.1'))
        self.now += 60
        self.assertTrue(self.owner(REPLACEMENT).exchange(REPLACEMENT, IP))

    def test_rate_limited_retries_do_not_write_unchanged_snapshots(self):
        for _ in range(self.auth.EXCHANGES_PER_IP_MINUTE):
            self.assert_error('invitation_invalid', lambda: self.auth.exchange('wrong', IP))
        writes = self.storage.writes
        for _ in range(10):
            self.assert_error('rate_limited', lambda: self.session())
        self.assertEqual(self.storage.writes, writes)
        for index in range(self.auth.EXCHANGES_PER_MINUTE - 5):
            self.assert_error('invitation_invalid', lambda: self.auth.exchange('wrong', f'198.51.100.{index}'))
        writes = self.storage.writes
        for index in range(10):
            self.assert_error('rate_limited', lambda: self.session(f'203.0.113.{index}'))
        self.assertEqual(self.storage.writes, writes)

    def test_session_and_global_create_limits_survive_reconstruction(self):
        tokens = [self.session(f'192.0.2.{index}') for index in range(5)]
        for token in tokens[:4]:
            for _ in range(self.auth.CREATES_PER_SESSION_MINUTE):
                self.auth.admit_create(token)
                self.auth = self.owner()
            self.assert_error('rate_limited', lambda: self.auth.admit_create(token))
        self.assert_error('rate_limited', lambda: self.auth.admit_create(tokens[4]))
        self.assertEqual(self.auth.authorize(tokens[0]), self.auth.invite_id)
        self.now += 60
        self.owner().admit_create(tokens[4])

    def test_session_capacity_is_preserved_and_expired_rows_are_pruned(self):
        for index in range(self.auth.MAX_SESSIONS):
            if index and index % 60 == 0:
                self.now += 60
            self.session(f'192.0.2.{index}')
        self.auth = self.owner()
        self.assert_error('rate_limited', lambda: self.session('198.51.100.1'))
        self.now += 900
        self.assertTrue(self.session())
        self.assertEqual(len(json.loads(self.stored())['sessions']), 1)

    def test_failed_exchange_write_never_publishes_new_session(self):
        self.storage.fail_write = True
        generated = 'n' * 43
        with patch('caller_auth.secrets.token_urlsafe', return_value=generated):
            error = self.assert_error('service_unavailable', lambda: self.session())
        self.assertEqual(error.status, 503)
        self.assertEqual(self.auth._sessions, {})
        self.assert_error('service_unavailable', lambda: self.auth.authorize(generated))
        self.storage.fail_write = False
        self.assert_error('session_expired', lambda: self.owner().authorize(generated))

    def test_failed_create_write_preserves_committed_counter_and_closes_auth(self):
        token = self.session()
        before = self.stored()
        self.storage.fail_write = True
        self.assert_error('service_unavailable', lambda: self.auth.admit_create(token))
        self.assertEqual(self.stored(), before)
        self.assertEqual(len(self.auth._creates), 0)
        self.assert_error('service_unavailable', lambda: self.auth.authorize(token))
        self.storage.fail_write = False
        restored = self.owner()
        for _ in range(3):
            restored.admit_create(token)
        self.assert_error('rate_limited', lambda: restored.admit_create(token))

    def test_failed_guess_write_returns_storage_error_without_losing_committed_limit(self):
        for _ in range(4):
            self.assert_error('invitation_invalid', lambda: self.auth.exchange('wrong', IP))
        self.storage.fail_write = True
        self.assert_error('service_unavailable', lambda: self.auth.exchange('wrong', IP))
        self.storage.fail_write = False
        self.auth = self.owner()
        self.assert_error('invitation_invalid', lambda: self.auth.exchange('wrong', IP))
        self.assert_error('rate_limited', lambda: self.session())

    def test_live_memory_is_unchanged_until_sql_write_completes(self):
        self.storage.before_write = lambda: self.assertEqual(self.auth._sessions, {})
        token = self.session()
        self.storage.before_write = lambda: self.assertEqual(len(self.auth._creates), 0)
        self.auth.admit_create(token)
        self.assertEqual(len(self.auth._creates), 1)

    def test_read_failure_and_rotation_write_failure_fail_closed(self):
        token = self.session()
        self.storage.fail_read = True
        broken = self.owner()
        self.assert_error('service_unavailable', lambda: broken.authorize(token))
        self.storage.fail_read = False
        self.storage.fail_write = True
        rotated = self.owner(REPLACEMENT)
        self.assert_error('service_unavailable', lambda: rotated.exchange(REPLACEMENT, IP))

    def test_invalid_persisted_shapes_fail_closed_without_rewriting(self):
        token = self.session()
        valid = json.loads(self.stored())
        variants = ['null', '[]', '{', '{"version":1,"version":1}', 'x' * 512001]
        changes = [dict(version=True), dict(version=2), dict(invite_id='not-a-digest'),
                   dict(saved_at=float('nan')), dict(saved_at=self.now + 901), dict(creates=[self.now + 1]),
                   dict(exchanges=[self.now] * 61), dict(ips={IP: [self.now]}),
                   dict(sessions={'x': {'expires_at': self.now + 900, 'creates': []}}),
                   dict(sessions={self.auth._digest(token).hex(): {'expires_at': self.now + 901, 'creates': []}}),
                   dict(extra='unexpected')]
        variants += [json.dumps(dict(valid, **change)) for change in changes]
        for raw in variants:
            with self.subTest(raw_length=len(raw)):
                self.sql.exec('UPDATE caller_auth_state SET value=? WHERE id=1', raw)
                broken = self.owner()
                self.assert_error('service_unavailable', lambda: broken.authorize(token))
                self.assert_error('service_unavailable', lambda: broken.exchange(INVITE, IP))
                self.assertEqual(self.stored(), raw)

    def test_auth_state_is_isolated_and_existing_tables_are_unchanged(self):
        journal = SqlJournal(self.sql)
        journal.write({'test': 'recovery-preserved'})
        journal.save_recent({'test-request': self.now + 600}, self.now)
        token = self.session()
        other_sql = SQLite()
        try:
            other = CloudCallerAuth(INVITE, other_sql, invite_expires_at=self.expiry, clock=lambda: self.now)
            self.assert_error('session_expired', lambda: other.authorize(token))
        finally:
            other_sql.connection.close()
        self.assertEqual(journal.read(), {'test': 'recovery-preserved'})
        self.assertEqual(journal.recent(), {'test-request': self.now + 600})


class CallRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_storage_failure_returns_fixed_http_error_without_blocking_device_poll(self):
        from types import SimpleNamespace
        from test_worker import worker, Storage
        sql, provider, storage = SQLite(), Provider(), Storage()
        fault = FaultSQL(sql)
        tasks = []
        instance = worker.StackChan.__new__(worker.StackChan)
        instance.ctx = SimpleNamespace(storage=storage, waitUntil=tasks.append)
        instance.ai = CloudManager(provider, lambda *args: 'fake-device-token', SqlJournal(sql))
        instance.calls = CloudCallManager(instance.ai, lambda *args: 'fake-device-token', lambda *args: {})
        instance.auth = CloudCallerAuth(INVITE, fault, invite_expires_at=253402300799)
        instance.api = CloudAPI(instance.ai, instance.calls, instance.auth,
                                'https://calls.example.test', 'd' * 40, before_activity=instance.arm)

        async def request(path, body, token=None, device=False):
            raw = json.dumps(body)
            headers = {'host': 'calls.example.test', 'content-type': 'application/json',
                       'content-length': str(len(raw.encode()))}
            if device:
                headers['authorization'] = 'Bearer ' + 'd' * 40
            else:
                headers['origin'] = 'https://calls.example.test'
                if token:
                    headers['authorization'] = 'Bearer ' + token
            async def text():
                return raw
            return await instance.fetch(SimpleNamespace(url='https://calls.example.test' + path,
                                                        method='POST', headers=headers, text=text))
        try:
            token = instance.auth.exchange(INVITE, IP)['access_token']
            idle = {'state': 'idle', 'call_id': '', 'ai_session_id': '', 'media_connected': False}
            self.assertEqual((await request('/device/poll', idle, device=True)).status, 200)
            fault.fail_write = True
            response = await request('/calls', {'request_id': '2' * 32, 'caller_name': 'Alex'}, token)
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(response.body), {'error': 'service_unavailable'})
            self.assertEqual(instance.calls.records, {})
            response = await request('/auth/session', {'invite': INVITE})
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(response.body), {'error': 'service_unavailable'})
            self.assertEqual((await request('/device/poll', idle, device=True)).status, 200)
            self.assertEqual(provider.starts, [])

            # A recovered auth store can admit a call; a later auth-only
            # failure must still permit that call's independent capability.
            fault.fail_write = False
            instance.auth = CloudCallerAuth(INVITE, fault, invite_expires_at=253402300799)
            instance.api.auth = instance.auth
            created = await request('/calls', {'request_id': '3' * 32, 'caller_name': 'Alex'}, token)
            self.assertEqual(created.status, 200)
            call = json.loads(created.body)
            fault.fail_write = True
            self.assertEqual((await request('/auth/session', {'invite': INVITE})).status, 503)
            for path in ('/calls/poll', '/calls/end'):
                response = await request(path, {'call_id': call['call_id']}, call['access_token'])
                self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.body)['call']['state'], 'ended')
        finally:
            pending = set(tasks + instance.calls.tasks())
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            sql.connection.close()

    async def test_caller_session_survives_but_old_call_capability_and_media_do_not(self):
        sql, provider, owners = SQLite(), Provider(), []
        now = 1800000000
        def owner():
            ai = CloudManager(provider, lambda *args: 'fake-device-token', SqlJournal(sql), clock=lambda: now)
            calls = CloudCallManager(ai, lambda *args: 'fake-device-token', lambda *args: {}, clock=lambda: now)
            auth = CloudCallerAuth(INVITE, sql, invite_expires_at=now + 86400, clock=lambda: now)
            owners.append(calls)
            return calls, auth, CloudAPI(ai, calls, auth, 'https://calls.example.test', 'd' * 40)
        try:
            calls, auth, _ = owner()
            await calls.device_poll({'state': 'idle', 'call_id': '', 'ai_session_id': '', 'media_connected': False})
            token = auth.exchange(INVITE, IP)['access_token']
            call = await calls.create('1' * 32, 'Alex', caller_auth=auth, caller_session=token)
            restored, restored_auth, api = owner()
            headers = {'host': 'calls.example.test', 'authorization': 'Bearer ' + token}
            status = await api.handle('GET', '/status', headers)
            self.assertEqual(status, {'device_online': False, 'busy': False})
            self.assertEqual(restored_auth.authorize(token), auth.invite_id)
            with self.assertRaises(SessionError) as caught:
                await restored.caller_poll(call['call_id'], call['access_token'])
            self.assertEqual(caught.exception.code, 'forbidden')
            response = await restored.device_poll({'state': 'human', 'call_id': call['call_id'],
                                                  'ai_session_id': '', 'media_connected': True})
            self.assertIsNone(response['call'])
            self.assertFalse(restored.recovered)
            raw = sql.exec('SELECT value FROM caller_auth_state WHERE id=1').toArray()[0].value
            self.assertNotIn(call['access_token'], raw)
            self.assertNotIn(call['call_id'], raw)
            self.assertNotIn('Alex', raw)
        finally:
            tasks = {task for calls in owners for task in calls.tasks()}
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            sql.connection.close()


if __name__ == '__main__':
    unittest.main()
