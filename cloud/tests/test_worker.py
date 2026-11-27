"""Offline handler fault injection; not a Workers runtime acceptance test.

The real handler/API/state machines run against SQLite and a fake provider.
Only Workers Response/context/alarm bindings are replaced, with explicit faults.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_state import SQLite, Provider
from api import CloudAPI
from caller_auth import CallerAuth
from state import CloudManager, CloudCallManager, SqlJournal


class Response:
    def __init__(self, body, *, status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers


class DurableObject:
    def __init__(self, ctx, env):
        self.ctx, self.env = ctx, env


spec = importlib.util.spec_from_file_location('worker_under_test',
    Path(__file__).resolve().parents[1] / 'worker.py')
worker = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, workers=SimpleNamespace(
        DurableObject=DurableObject, WorkerEntrypoint=object, Response=Response)):
    spec.loader.exec_module(worker)


class Storage:
    def __init__(self):
        self.deadline = None
        self.fail = set()
        self.sets = 0

    async def getAlarm(self):
        if 'get' in self.fail:
            raise RuntimeError('test private storage detail')
        return self.deadline

    async def setAlarm(self, value):
        if 'set' in self.fail:
            raise RuntimeError('test private storage detail')
        self.deadline = value
        self.sets += 1

    async def deleteAlarm(self):
        if 'delete' in self.fail:
            raise RuntimeError('test private storage detail')
        self.deadline = None


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sql, self.provider, self.storage = SQLite(), Provider(), Storage()
        self.tasks = []
        self.owner = worker.StackChan.__new__(worker.StackChan)
        self.owner.ctx = SimpleNamespace(storage=self.storage, waitUntil=self.tasks.append)
        self.owner.ai = CloudManager(self.provider, lambda *args: 'test-token', SqlJournal(self.sql))
        self.owner.calls = CloudCallManager(self.owner.ai,
            lambda *args: 'test-token', lambda *args: {})
        self.owner.api = CloudAPI(self.owner.ai, self.owner.calls,
            CallerAuth('i' * 40), 'https://calls.example.test', 'd' * 40,
            before_activity=self.owner.arm)
        await self.owner.calls.device_poll({'state': 'idle', 'call_id': '',
            'ai_session_id': '', 'media_connected': False})

    async def asyncTearDown(self):
        tasks = set(self.tasks + self.owner.calls.tasks())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.sql.connection.close()

    async def request(self, path='/ai/start', body=None, *, authorized=True):
        raw = json.dumps(body or {'request_id': 'request_ai_test'})
        headers = {'host': 'calls.example.test', 'content-type': 'application/json',
            'content-length': str(len(raw.encode())),
            'authorization': 'Bearer ' + ('d' * 40 if authorized else 'wrong')}
        async def text():
            return raw
        return await self.owner.fetch(SimpleNamespace(url='https://calls.example.test' + path,
            method='POST', headers=headers, text=text))

    async def test_frontend_assets_are_explicitly_served_with_correct_types(self):
        entry = worker.Default()
        entry.env = SimpleNamespace(PUBLIC_ORIGIN='https://calls.example.test')
        for route, name, kind in [('/', 'caller.html', 'text/html; charset=utf-8'),
                                  ('/stackchan.png', 'stackchan.png', 'image/png'),
                                  ('/lines.png', 'lines.png', 'image/png')]:
            def read(path):
                self.assertEqual(path.name, name)
                return b'asset-content'
            with patch.object(Path, 'read_bytes', read):
                response = await entry.fetch(SimpleNamespace(
                    url='https://calls.example.test' + route, method='GET'))
            self.assertEqual(response.body, b'asset-content')
            self.assertEqual(response.headers['Content-Type'], kind)
        response = await entry.fetch(SimpleNamespace(
            url='https://calls.example.test/private/bindings.json', method='GET'))
        self.assertEqual(response.status, 404)

    async def test_join_has_alarm_before_provider_io_and_grant_is_preserved(self):
        self.provider.on_start = lambda _: self.assertIsNotNone(self.storage.deadline)
        response = await self.request()
        self.assertEqual(response.status, 200)
        self.assertIn('whip_token', json.loads(response.body))
        self.assertIsNotNone(self.storage.deadline)

    async def test_constructor_uses_password_secret_without_legacy_invite_bindings(self):
        self.storage.sql = self.sql
        env = SimpleNamespace(AGORA_APP_ID='a' * 32, AGORA_APP_CERTIFICATE='b' * 32,
            DEEPSEEK_API_KEY='synthetic-deepseek', FISH_AUDIO_API_KEY='synthetic-fish',
            CALLER_PASSWORD='synthetic-password', PUBLIC_ORIGIN='https://calls.example.test',
            DEVICE_KEY='d' * 40)
        instance = worker.StackChan(SimpleNamespace(storage=self.storage, waitUntil=self.tasks.append), env)
        config = await instance.api.handle('GET', '/auth/config', {'host': 'calls.example.test'})
        self.assertEqual(config, {'required': True, 'mode': 'password'})
        token = instance.auth.exchange(env.CALLER_PASSWORD, 'test-client')['access_token']
        self.assertEqual(instance.auth.authorize(token), instance.auth.invite_id)
        self.assertEqual(instance.auth.SESSION_SECONDS, 8 * 3600)

    async def test_critical_alarm_failure_prevents_join(self):
        for operation in ('get', 'set'):
            with self.subTest(operation=operation):
                self.storage.fail = {operation}
                response = await self.request()
                self.assertEqual(response.status, 503)
                self.assertEqual(json.loads(response.body), {'error': 'service_unavailable'})
                self.assertEqual(self.provider.starts, [])
                self.assertIsNone(self.owner.ai._record)

    async def test_tick_failure_preserves_success_with_durable_recovery(self):
        async def fail_tick():
            raise RuntimeError('test private tick detail')
        self.owner.calls.tick = fail_tick
        with patch('builtins.print') as log:
            response = await self.request()
        self.assertEqual(response.status, 200)
        self.assertIsNotNone(self.storage.deadline)
        self.assertNotIn('private', str(log.call_args_list))

    async def test_failed_maintenance_and_rearm_withholds_successful_grant(self):
        async def fail_tick():
            self.storage.fail.add('get')
            raise RuntimeError('tick failed')
        self.owner.calls.tick = fail_tick
        response = await self.request()
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.body), {'error': 'service_unavailable'})
        self.assertIsNotNone(SqlJournal(self.sql).read())
        self.assertIsNotNone(self.storage.deadline)

    async def test_maintenance_failure_does_not_mask_auth_error(self):
        async def fail_tick():
            raise RuntimeError('tick failed')
        self.owner.calls.tick = fail_tick
        self.storage.fail = {'set'}
        response = await self.request(authorized=False)
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(response.body), {'error': 'forbidden'})
        self.assertEqual(self.provider.starts, [])

    async def test_delete_failure_rearms_without_masking_validation_error(self):
        self.storage.fail = {'delete'}
        response = await self.request(body={'unexpected': True})
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body), {'error': 'invalid_request'})
        self.assertIsNotNone(self.storage.deadline)

    async def test_alarm_tick_failure_leaves_next_wakeup_and_recovers(self):
        await self.request()
        self.storage.deadline = None  # Running alarm is consumed by the runtime.
        original_tick = self.owner.calls.tick
        async def fail_tick():
            raise RuntimeError('tick failed')
        self.owner.calls.tick = fail_tick
        with self.assertRaises(RuntimeError):
            await self.owner.alarm()
        self.assertIsNotNone(self.storage.deadline)
        self.owner.calls.tick = original_tick
        self.owner.ai._record['lease_until'] = 0
        await self.owner.alarm()
        await asyncio.gather(*self.tasks)
        await self.owner.alarm()
        self.assertIsNone(self.owner.ai._record)
        self.assertIsNone(self.storage.deadline)

    async def test_frequent_maintenance_does_not_postpone_existing_alarm(self):
        await self.request()
        deadline, count = self.storage.deadline, self.storage.sets
        await self.owner.maintain()
        await self.owner.maintain()
        self.assertEqual(self.storage.deadline, deadline)
        self.assertEqual(self.storage.sets, count)

    async def test_alarm_storage_failure_does_not_block_device_end_or_reject(self):
        for action in ('end', 'reject'):
            with self.subTest(action=action):
                call = await self.owner.calls.create(('a' if action == 'end' else 'b') * 32, 'Alex')
                self.storage.fail = {'get', 'set'}
                response = await self.request('/device/call',
                    {'call_id': call['call_id'], 'action': action})
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.body)['call']['state'], 'ended')
                self.assertEqual(self.owner.calls.current['reason'],
                    'device_hangup' if action == 'end' else 'rejected')
                self.storage.fail.clear()
                await self.owner.calls.device_poll({'state': 'idle', 'call_id': '',
                    'ai_session_id': '', 'media_connected': False})

    async def test_alarm_storage_failure_does_not_block_ai_stop(self):
        await self.request()
        self.storage.fail = {'get', 'set'}
        response = await self.request('/ai/stop')
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)['state'], 'stopped')
        self.assertEqual(len(self.provider.stops), 1)
        self.assertIsNone(self.owner.ai._record)

    async def test_expired_caller_session_is_rejected_before_alarm_storage(self):
        self.storage.fail = {'get', 'set'}
        raw = json.dumps({'request_id': 'a' * 32, 'caller_name': 'Alex'})
        async def text():
            return raw
        response = await self.owner.fetch(SimpleNamespace(
            url='https://calls.example.test/calls', method='POST', text=text,
            headers={'host': 'calls.example.test', 'origin': 'https://calls.example.test',
                'content-type': 'application/json', 'content-length': str(len(raw)),
                'authorization': 'Bearer ' + 'x' * 40}))
        self.assertEqual(response.status, 401)
        self.assertEqual(json.loads(response.body), {'error': 'session_expired'})
        self.assertIsNone(self.owner.calls.current)


if __name__ == '__main__':
    unittest.main()
