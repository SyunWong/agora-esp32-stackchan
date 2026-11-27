"""Cloud-only durable caller sessions; human-call capabilities stay in memory.

The bounded snapshot contains only digests, expirations and rate-limit times.
Each mutation runs on a private copy, commits one SQLite row, then publishes the
copy. There are no await points or keepalive timers in this adapter.
"""
from collections import deque
from contextlib import nullcontext
from copy import copy
import hashlib
import json
import math
import re
import time

from caller_auth import CallerAuth
from stackchan_ai import SessionError


class CloudCallerAuth(CallerAuth):
    AUTH_TABLE = 'caller_auth_state'
    MAX_JOURNAL_BYTES = 512_000
    MAX_TIMESTAMP = 253402300799
    STATE_FIELDS = ('_sessions', '_ips', '_exchanges', '_creates')

    def __init__(self, invite_secret, sql, *, invite_expires_at, clock=time.time):
        super().__init__(invite_secret, invite_expires_at=invite_expires_at,
                         clock=clock, lock_factory=nullcontext)
        self._sql, self._unavailable = sql, False
        try:
            sql.exec(f'CREATE TABLE IF NOT EXISTS {self.AUTH_TABLE} '
                     '(id INTEGER PRIMARY KEY CHECK (id=1), value TEXT NOT NULL)')
            rows = sql.exec(f'SELECT value FROM {self.AUTH_TABLE} WHERE id=1').toArray()
            if len(rows):
                row = self._decode(rows[0].value)
                self._restore(row)
                if (row['invite_id'] != self.invite_id
                        or row['invite_expires_at'] != self.invite_expires_at):
                    # Replacing the invite or its expiry revokes sessions, even
                    # if a later deployment restores the old configuration.
                    # Device-wide abuse limits survive this rotation too.
                    self._sessions = {}
                    self._sweep(self._clock())
                    self._write(self)
        except Exception:
            # Auth storage failures must not take down device polling/media
            # cleanup. Caller authorization stays closed until reconstruction.
            self._unavailable = True

    @classmethod
    def _timestamp(cls, value):
        return (type(value) in (int, float) and math.isfinite(value)
                and 0 <= value <= cls.MAX_TIMESTAMP)

    @staticmethod
    def _hex_digest(value):
        return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None

    @staticmethod
    def _unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('invalid_caller_auth_journal')
            result[key] = value
        return result

    def _decode(self, raw):
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > self.MAX_JOURNAL_BYTES:
            raise ValueError('invalid_caller_auth_journal')
        row = json.loads(raw, object_pairs_hook=self._unique_object)
        fields = {'version', 'invite_id', 'invite_expires_at', 'saved_at',
                  'sessions', 'ips', 'exchanges', 'creates'}
        if (not isinstance(row, dict) or set(row) != fields
                or type(row['version']) is not int or row['version'] != 1
                or not self._hex_digest(row['invite_id'])
                or type(row['invite_expires_at']) is not int
                or not self._timestamp(row['invite_expires_at'])
                or not self._timestamp(row['saved_at']) or row['saved_at'] > self._clock()):
            raise ValueError('invalid_caller_auth_journal')

        def bucket(value, limit):
            if (not isinstance(value, list) or len(value) > limit
                    or any(not self._timestamp(stamp) or stamp > row['saved_at'] for stamp in value)
                    or value != sorted(value)):
                raise ValueError('invalid_caller_auth_journal')

        bucket(row['exchanges'], self.EXCHANGES_PER_MINUTE)
        bucket(row['creates'], self.CREATES_PER_MINUTE)
        if (not isinstance(row['sessions'], dict) or len(row['sessions']) > self.MAX_SESSIONS
                or not isinstance(row['ips'], dict) or len(row['ips']) > self.MAX_IP_BUCKETS):
            raise ValueError('invalid_caller_auth_journal')
        for digest, session in row['sessions'].items():
            if (not self._hex_digest(digest) or not isinstance(session, dict)
                    or set(session) != {'expires_at', 'creates'}
                    or type(session['expires_at']) is not int
                    or not 0 <= session['expires_at'] <= min(
                        row['invite_expires_at'], int(row['saved_at']) + self.SESSION_SECONDS)):
                raise ValueError('invalid_caller_auth_journal')
            bucket(session['creates'], self.CREATES_PER_SESSION_MINUTE)
        for digest, stamps in row['ips'].items():
            if not self._hex_digest(digest):
                raise ValueError('invalid_caller_auth_journal')
            bucket(stamps, self.EXCHANGES_PER_IP_MINUTE)
        return row

    def _restore(self, row):
        self._sessions = {bytes.fromhex(key): {'expires_at': value['expires_at'],
                          'creates': deque(value['creates'])} for key, value in row['sessions'].items()}
        self._ips = {bytes.fromhex(key): deque(value) for key, value in row['ips'].items()}
        self._exchanges, self._creates = deque(row['exchanges']), deque(row['creates'])

    def _write(self, owner):
        row = {'version': 1, 'invite_id': owner.invite_id,
               'invite_expires_at': owner.invite_expires_at, 'saved_at': owner._clock(),
               'sessions': {key.hex(): {'expires_at': value['expires_at'],
                            'creates': list(value['creates'])} for key, value in owner._sessions.items()},
               'ips': {key.hex(): list(value) for key, value in owner._ips.items()},
               'exchanges': list(owner._exchanges), 'creates': list(owner._creates)}
        raw = json.dumps(row, separators=(',', ':'), allow_nan=False)
        self._decode(raw)  # Enforce the same bounded schema on writes and reads.
        self._sql.exec(f'INSERT INTO {self.AUTH_TABLE} (id,value) VALUES (1,?) '
                       'ON CONFLICT(id) DO UPDATE SET value=excluded.value', raw)

    def _check_available(self):
        if self._unavailable:
            raise SessionError('service_unavailable', 503)

    def _mutate(self, operation, *args):
        self._check_available()
        candidate = copy(self)
        candidate._sessions = {key: {'expires_at': value['expires_at'], 'creates': deque(value['creates'])}
                               for key, value in self._sessions.items()}
        candidate._ips = {key: deque(value) for key, value in self._ips.items()}
        candidate._exchanges, candidate._creates = deque(self._exchanges), deque(self._creates)
        candidate._sweep(candidate._clock())
        error, result = None, None
        try:
            result = operation(candidate, *args)
        except SessionError as caught:
            # Wrong invite guesses consume exchange limits too. Persist those
            # changes before returning the normal fixed authentication error.
            error = caught
        # A rejected exchange can leave a newly created empty IP bucket. Drop
        # it before comparison so repeated 429s cannot amplify SQLite writes.
        candidate._sweep(candidate._clock())
        if all(getattr(candidate, name) == getattr(self, name) for name in self.STATE_FIELDS):
            if error is not None:
                raise error
            return result
        try:
            self._write(candidate)
        except Exception:
            self._unavailable = True
            raise SessionError('service_unavailable', 503) from None
        for name in self.STATE_FIELDS:
            setattr(self, name, getattr(candidate, name))
        if error is not None:
            raise error
        return result

    def exchange(self, invite, client_ip):
        return self._mutate(CallerAuth.exchange, invite, client_ip)

    def admit_create(self, token):
        return self._mutate(CallerAuth.admit_create, token)

    def authorize(self, token):
        self._check_available()
        return super().authorize(token)


class CloudPasswordAuth(CloudCallerAuth):
    """Shared-password login with durable eight-hour caller sessions.

    Passwords are exact UTF-8 strings: no trimming or case folding. Domain
    separation converts the bounded server-side input into the legacy random-
    credential format without weakening caller-session token validation. Only
    its digest reaches the journal; raw passwords are neither kept nor logged.
    A separate table preserves compatibility with the old 15-minute reader.
    """
    AUTH_MODE = 'password'
    AUTH_TABLE = 'caller_password_auth_state'
    SESSION_SECONDS = 8 * 60 * 60

    @staticmethod
    def _password_credential(password):
        if not isinstance(password, str) or not 0 < len(password) <= 128:
            return None
        try:
            encoded = password.encode('utf-8')
        except UnicodeError:
            return None
        if len(encoded) > 256:
            return None
        return hashlib.sha256(b'stackchan/caller-password/v1\0' + encoded).hexdigest()

    def __init__(self, password, sql, *, clock=time.time):
        credential = self._password_credential(password)
        if credential is None:
            raise ValueError('caller_password_required')
        # The shared password has no invitation-style calendar expiry. Session
        # TTL and rotation still revoke authorization through the existing code.
        super().__init__(credential, sql, invite_expires_at=self.MAX_TIMESTAMP, clock=clock)

    def exchange(self, password, client_ip):
        credential = self._password_credential(password)
        try:
            # Invalid input follows the same persisted failed-guess limiter.
            return super().exchange(credential or '', client_ip)
        except SessionError as error:
            if error.code == 'invitation_invalid':
                raise SessionError('password_invalid', 401) from None
            raise
