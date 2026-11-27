"""HTTPS caller + device API, with exactly one Durable Object per StackChan."""
import asyncio
import gzip
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from workers import DurableObject, Response, WorkerEntrypoint
from caller_sessions import CloudPasswordAuth
from stackchan_ai import SessionError
from api import CloudAPI, DEVICE_PATHS, CALLER_PATHS
from state import CloudManager, CloudCallManager, SqlJournal, finish_tasks
from provider import CloudProvider
from token_helper import whip_token, rtc_token

HEADERS = {
    'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
    'Permissions-Policy': 'microphone=(self), camera=()',
}


def reply(value, status=200):
    headers = dict(HEADERS, **{'Content-Type': 'application/json'})
    if status == 429:
        headers['Retry-After'] = '60'
    return Response(json.dumps(value, separators=(',', ':')), status=status, headers=headers)


def request_headers(request):
    names = ('host', 'origin', 'authorization', 'content-type', 'content-length',
             'transfer-encoding', 'sec-fetch-site')
    return {name: request.headers.get(name) for name in names if request.headers.get(name) is not None}


class StackChan(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        provider = CloudProvider(env.AGORA_APP_ID, env.AGORA_APP_CERTIFICATE,
            deepseek_api_key=env.DEEPSEEK_API_KEY, fish_audio_api_key=env.FISH_AUDIO_API_KEY)
        make_device = lambda channel, expiry: whip_token(env.AGORA_APP_ID, env.AGORA_APP_CERTIFICATE, channel, expiry)
        make_caller = lambda channel, expiry: rtc_token(env.AGORA_APP_ID, env.AGORA_APP_CERTIFICATE, channel, expiry)
        self.ai = CloudManager(provider, make_device, SqlJournal(ctx.storage.sql))
        self.calls = CloudCallManager(self.ai, make_device, make_caller,
            on_event=lambda event: print('Call event: ' + event))
        self.auth = CloudPasswordAuth(env.CALLER_PASSWORD, ctx.storage.sql)
        self.api = CloudAPI(self.ai, self.calls, self.auth, env.PUBLIC_ORIGIN, env.DEVICE_KEY,
                            before_activity=self.arm)

    async def maintain(self):
        # Commit the next wakeup before tick can start provider work or raise.
        # In an alarm handler getAlarm() is unset, so a failing tick still has
        # a durable retry rather than relying only on platform retry limits.
        if self.calls.needs_alarm():
            await self.arm()
        await self.calls.tick()
        tasks = self.calls.tasks()
        if tasks:
            self.ctx.waitUntil(asyncio.create_task(finish_tasks(tasks)))
        if self.calls.needs_alarm():
            # Keep the earlier alarm; frequent polls must not postpone cleanup.
            previous = await self.ctx.storage.getAlarm()
            deadline = int(time.time() * 1000) + 5000
            if not previous or int(previous) > deadline:
                await self.ctx.storage.setAlarm(deadline)
        else:
            await self.ctx.storage.deleteAlarm()

    async def arm(self):
        previous = await self.ctx.storage.getAlarm()
        deadline = int(time.time() * 1000) + 5000
        if not previous or int(previous) > deadline:
            await self.ctx.storage.setAlarm(deadline)

    async def alarm(self):
        await self.maintain()

    async def maintain_response(self, response):
        """Maintenance must not accidentally replace a protocol response.

        Preserve it if recovery has a durable wakeup. If storage cannot even
        schedule recovery, withhold successful grants with a fixed 503 instead
        of leaking an exception. Existing auth/validation errors stay intact.
        """
        try:
            await self.maintain()
        except Exception:
            print('Control maintenance failed: maintenance_failed')
            try:
                await self.arm()
            except Exception:
                print('Control maintenance failed: recovery_schedule_failed')
                if response.status < 400:
                    return reply({'error': 'service_unavailable'}, 503)
        return response

    async def fetch(self, request):
        path = urlsplit(request.url).path
        headers = request_headers(request)
        try:
            self.api.authorize_transport(request.method, path, headers)
            raw = b''
            if request.method == 'POST':
                try:
                    length = int(headers.get('content-length', '0'))
                except ValueError:
                    length = 0
                if not 0 < length <= 512:
                    raise SessionError('invalid_request', 400)
                raw = (await request.text()).encode('utf-8')
                if len(raw) != length:
                    raise SessionError('invalid_request', 400)
            result = await self.api.handle(request.method, path, headers, raw,
                client_ip=request.headers.get('CF-Connecting-IP') or 'unknown')
            response = reply(result)
        except SessionError as error:
            response = reply({'error': error.code}, error.status)
        except Exception as error:
            print('Control request failed: ' + type(error).__name__)
            response = reply({'error': 'service_unavailable'}, 503)
        return await self.maintain_response(response)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlsplit(request.url)
        if url.scheme != 'https' or 'https://' + url.netloc != self.env.PUBLIC_ORIGIN or url.query:
            return reply({'error': 'not_found'}, 404)
        assets = {'/': ('caller.html', 'text/html; charset=utf-8'),
                  '/agora.js': ('agora.js.gz', 'text/javascript; charset=utf-8'),
                  '/stackchan.png': ('stackchan.png', 'image/png'),
                  '/lines.png': ('lines.png', 'image/png')}
        if request.method == 'GET' and url.path in assets:
            name, kind = assets[url.path]
            content = (Path(__file__).parent / name).read_bytes()
            headers = dict(HEADERS, **{'Content-Type': kind})
            if url.path == '/agora.js':
                # The package is compressed to keep uploads small. Let the edge
                # choose HTTP content encoding; precompressed Response defaults
                # otherwise compress the same bytes twice.
                content = gzip.decompress(content)
            return Response(content, headers=headers)
        if request.method == 'GET' and url.path == '/healthz':
            return reply({'ok': True, 'service': 'stackchan-cloud', 'revision': self.env.RELEASE,
                          'enabled': self.env.ENABLED == 'true'})
        if url.path not in DEVICE_PATHS | CALLER_PATHS | {'/status', '/auth/config'}:
            return reply({'error': 'not_found'}, 404)
        if self.env.ENABLED != 'true':
            return reply({'error': 'service_not_enabled'}, 503)
        stub = self.env.STACKCHAN.getByName(self.env.DEVICE_ID)
        return await stub.fetch(request)
