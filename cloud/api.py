"""Small transport adapter; protocol validation and state ownership stay separate."""
import hmac
import json
from urllib.parse import urlsplit

from stackchan_ai import SessionError

DEVICE_PATHS = frozenset(('/device/poll', '/device/call', '/ai/start', '/ai/stop', '/ai/heartbeat'))
CALLER_PATHS = frozenset(('/auth/session', '/calls', '/calls/poll', '/calls/end'))


class CloudAPI:
    def __init__(self, ai, calls, auth, origin, device_key, *, before_activity=None):
        url = urlsplit(origin)
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.path or url.query or url.fragment or url.netloc != url.netloc.lower()):
            raise ValueError('invalid_public_origin')
        if not isinstance(device_key, str) or len(device_key) < 32:
            raise ValueError('invalid_device_key')
        self.ai, self.calls, self.auth = ai, calls, auth
        self.origin, self.host = origin, url.netloc
        self.expected = ('Bearer ' + device_key).encode()
        self.before_activity = before_activity

    def authorize_transport(self, method, path, headers):
        if headers.get('host') != self.host:
            raise SessionError('forbidden', 403)
        if path in DEVICE_PATHS:
            if (method != 'POST' or headers.get('origin') is not None
                    or headers.get('sec-fetch-site') is not None
                    or not hmac.compare_digest(headers.get('authorization', '').encode(), self.expected)):
                raise SessionError('forbidden', 403)
        elif path in CALLER_PATHS or path in ('/status', '/auth/config'):
            allowed = (self.origin,) if method == 'POST' else (None, self.origin)
            if (headers.get('origin') not in allowed
                    or headers.get('sec-fetch-site') not in (None, 'none', 'same-origin')):
                raise SessionError('forbidden', 403)
        else:
            raise SessionError('not_found', 404)

    async def handle(self, method, path, headers, raw=b'', client_ip='unknown'):
        self.authorize_transport(method, path, headers)
        password_mode = getattr(self.auth, 'AUTH_MODE', None) == 'password'
        credential_field = 'password' if password_mode else 'invite'
        token = headers.get('authorization', '')
        token = token[7:] if token.startswith('Bearer ') else ''
        if method == 'GET':
            if path == '/auth/config':
                return {'required': True, 'mode': 'password'} if password_mode else {'required': True}
            if path == '/status':
                self.auth.authorize(token)
                return await self.calls.status()
            raise SessionError('not_found', 404)
        if method != 'POST':
            raise SessionError('method_not_allowed', 405)
        if (headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json'
                or headers.get('transfer-encoding')):
            raise SessionError('json_required', 415)
        if not 0 < len(raw) <= (256 if path in DEVICE_PATHS else 512):
            raise SessionError('invalid_request', 400)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise SessionError('invalid_request', 400) from None
        shapes = {
            '/device/poll': {'state', 'ai_session_id', 'call_id', 'media_connected'},
            '/device/call': {'call_id', 'action'}, '/ai/start': {'request_id'},
            '/ai/heartbeat': {'session_id'}, '/ai/stop': {'request_id'} if isinstance(value, dict) and 'request_id' in value else {'session_id'},
            '/auth/session': {credential_field}, '/calls': {'request_id', 'caller_name'},
            '/calls/poll': {'call_id'}, '/calls/end': {'call_id'},
        }
        if path not in shapes or not isinstance(value, dict) or set(value) != shapes[path]:
            raise SessionError('invalid_request', 400)
        if path in DEVICE_PATHS and path != '/device/poll' and any(not isinstance(item, str) for item in value.values()):
            raise SessionError('invalid_request', 400)
        if path == '/calls':
            # Reject expired sessions before touching alarm storage. create()
            # rechecks authorization after the awaited recovery hook below.
            self.auth.authorize(token)
        if self.before_activity is not None and (
                path in ('/ai/start', '/calls') or
                (path == '/device/call' and value['action'] in ('accept', 'ready', 'connected'))):
            # Establish recovery before new activity. Ending/rejecting activity
            # must still run when alarm storage is unavailable.
            await self.before_activity()
        if path == '/auth/session':
            return self.auth.exchange(value[credential_field], client_ip)
        if path == '/calls':
            return await self.calls.create(value['request_id'], value['caller_name'], caller_auth=self.auth, caller_session=token)
        if path == '/calls/poll':
            return await self.calls.caller_poll(value['call_id'], token)
        if path == '/calls/end':
            return await self.calls.caller_end(value['call_id'], token)
        if path == '/device/poll':
            return await self.calls.device_poll(value)
        if path == '/device/call':
            return await self.calls.device_action(value['call_id'], value['action'])
        if path == '/ai/start':
            return await self.ai.start(value['request_id'])
        if path == '/ai/heartbeat':
            return await self.ai.heartbeat(value['session_id'])
        return await self.ai.stop(**value)
