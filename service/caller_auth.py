"""Optional public-caller access. Credentials stay in memory as hashes only.

Use a cryptographically generated URL-safe invite secret (for example,
secrets.token_urlsafe(32)). Replacing this instance/secret revokes its sessions.
No device credential, RTC credential, audio, or caller name belongs here.
"""
from collections import deque
import hashlib
import hmac
import math
import re
import secrets
import time

from stackchan_ai import SessionError


class CallerAuth:
    SESSION_SECONDS = 15 * 60
    INVITE_SECONDS = 30 * 24 * 60 * 60
    EXCHANGES_PER_IP_MINUTE = 5
    EXCHANGES_PER_MINUTE = 60
    CREATES_PER_SESSION_MINUTE = 3
    CREATES_PER_MINUTE = 12
    MAX_SESSIONS = 256
    MAX_IP_BUCKETS = 2048

    def __init__(self, invite_secret, *, invite_expires_at=None, clock=time.time, lock_factory=None):
        """Use lock_factory=contextlib.nullcontext only in a single-threaded owner.

        Every method is synchronous with no await points. The default lazily
        imports threading and keeps the local threaded HTTP server protected.
        """
        if not self._token(invite_secret):
            raise ValueError('invite_secret_must_be_random_urlsafe_32_to_256_characters')
        self._clock = clock
        expires = clock() + self.INVITE_SECONDS if invite_expires_at is None else invite_expires_at
        if type(expires) not in (int, float) or not math.isfinite(expires) or expires < 0:
            raise ValueError('invalid_invite_expiration')
        self._invite_digest = self._digest(invite_secret)
        self.invite_id = self._invite_digest.hex()
        self.invite_expires_at = int(expires)
        self._sessions = {}
        self._ips = {}
        self._exchanges = deque()
        self._creates = deque()
        if lock_factory is None:
            from threading import Lock
            lock_factory = Lock
        self._lock = lock_factory()

    @staticmethod
    def _token(value):
        return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{32,256}', value))

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode('utf-8')).digest()

    @staticmethod
    def _trim(bucket, now):
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()

    def _sweep(self, now):
        self._sessions = {key: value for key, value in self._sessions.items() if value['expires_at'] > now}
        for key, bucket in list(self._ips.items()):
            self._trim(bucket, now)
            if not bucket:
                del self._ips[key]
        self._trim(self._exchanges, now)
        self._trim(self._creates, now)

    def exchange(self, invite, client_ip):
        """Rate limits include failed guesses. client_ip must come from trusted transport."""
        with self._lock:
            now = self._clock()
            self._sweep(now)
            if not isinstance(client_ip, str) or not 0 < len(client_ip) <= 128:
                raise SessionError('forbidden', 403)
            key = self._digest(client_ip)
            bucket = self._ips.get(key)
            if bucket is None:
                if len(self._ips) >= self.MAX_IP_BUCKETS:
                    raise SessionError('rate_limited', 429)
                bucket = self._ips[key] = deque()
            if len(bucket) >= self.EXCHANGES_PER_IP_MINUTE or len(self._exchanges) >= self.EXCHANGES_PER_MINUTE:
                raise SessionError('rate_limited', 429)
            bucket.append(now)
            self._exchanges.append(now)
            if not self._token(invite) or not hmac.compare_digest(self._digest(invite), self._invite_digest):
                raise SessionError('invitation_invalid', 401)
            if now >= self.invite_expires_at:
                raise SessionError('invitation_expired', 401)
            if len(self._sessions) >= self.MAX_SESSIONS:
                raise SessionError('rate_limited', 429)
            token = secrets.token_urlsafe(32)
            expires = min(int(now) + self.SESSION_SECONDS, self.invite_expires_at)
            self._sessions[self._digest(token)] = {'expires_at': expires, 'creates': deque()}
            return {'access_token': token, 'expires_at': expires}

    def _session(self, token, now):
        if not self._token(token):
            raise SessionError('session_required', 401)
        row = self._sessions.get(self._digest(token))
        if row is None or now >= row['expires_at'] or now >= self.invite_expires_at:
            raise SessionError('session_expired', 401)
        return row

    def authorize(self, token):
        """Return the invite's stable idempotency scope, never the secret itself."""
        with self._lock:
            self._session(token, self._clock())
            return self.invite_id

    def admit_create(self, token):
        """Called only for a new call by its state owner; idempotent retries skip it."""
        with self._lock:
            now = self._clock()
            row = self._session(token, now)
            self._trim(row['creates'], now)
            self._trim(self._creates, now)
            if len(row['creates']) >= self.CREATES_PER_SESSION_MINUTE or len(self._creates) >= self.CREATES_PER_MINUTE:
                raise SessionError('rate_limited', 429)
            row['creates'].append(now)
            self._creates.append(now)
