"""Stdlib-only token adapters using the project's official Agora token builder."""
import base64
import hashlib
import hmac
import json
import re
import time

from agora_token.RtcTokenBuilder2 import RtcTokenBuilder, Role_Publisher


def validate_credentials(app_id, certificate):
    if any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{32}', value)
           for value in (app_id, certificate)):
        raise ValueError('invalid_agora_credentials')


def convo_token(app_id, certificate, channel, uid='20003', ttl=60):
    validate_credentials(app_id, certificate)
    if type(ttl) is not int or not 1 <= ttl <= 86400:
        raise ValueError('invalid_token_expiry')
    # Explicit privilege TTL matches the SDK's default resolution (0 -> ttl).
    return RtcTokenBuilder.build_token_with_rtm(
        app_id, certificate, channel, str(uid), Role_Publisher, ttl, ttl)


def whip_token(app_id, certificate, channel, expiry):
    validate_credentials(app_id, certificate)
    def b64(value):
        return base64.urlsafe_b64encode(value).rstrip(b'=').decode()
    header = b64(b'{"alg":"HS256","typ":"JWT"}')
    payload = b64(json.dumps({'version': '1.0', 'appID': app_id, 'streamID': channel,
        'exp': expiry, 'action': 'pub', 'enableSubAuth': True}, separators=(',', ':')).encode())
    unsigned = header + '.' + payload
    return unsigned + '.' + b64(hmac.new(certificate.encode(), unsigned.encode(), hashlib.sha256).digest())


def rtc_token(app_id, certificate, channel, expiry, *, clock=time.time):
    """Same string-UID caller grant as the local ai_server, with remaining TTL."""
    validate_credentials(app_id, certificate)
    ttl = min(600, int(expiry) - int(clock()))
    if ttl <= 0:
        raise ValueError('call_expired')
    return {'appId': app_id, 'channel': channel, 'uid': '20002', 'robotUid': '20001',
        'token': RtcTokenBuilder.build_token_with_user_account(app_id, certificate,
            channel, '20002', Role_Publisher, ttl, ttl), 'expiresAt': expiry, 'ttl': ttl}
