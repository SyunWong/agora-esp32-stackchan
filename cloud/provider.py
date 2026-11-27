"""Fetch-based Agora adapter for Python Workers; no SDK, sockets or threads.

The join mapping is tested against the installed, proven agora-agents 2.12.0
wire payload. Lifecycle paths follow the current main Agora API reference:
https://docs.agora.io/en/api-reference/api-ref/conversational-ai/join
No request, response, error detail, token or transcript is logged or persisted.
The DO owner supplies durable intent/recovery and retries; POSTs are not retried.
"""
import asyncio
import json
import math
from urllib.parse import quote, urlencode

from agent_profile import AGENT_UID, DEVICE_UID, FAILURE_MESSAGE, GREETING, PROMPT, STT_LANGUAGE
from token_helper import convo_token, validate_credentials

BASE_URL = 'https://api.agora.io/api/conversational-ai-agent/v2/projects/'
MAX_RESPONSE_BYTES = 262144


class ProviderError(RuntimeError):
    """Only fixed codes and numeric HTTP status; never raw upstream errors."""
    def __init__(self, code, upstream_status=None):
        super().__init__(code)
        self.code = code
        self.status = 503
        self.upstream_status = upstream_status


async def _workers_fetch(url, *, timeout, **options):
    from workers import fetch
    from js import AbortSignal
    return await fetch(url, signal=AbortSignal.timeout(int(timeout * 1000)), **options)


async def _bounded_json(response):
    """Read at most 256 KiB from Workers' native ReadableStream.

    Check a JavaScript chunk's length before copying it into Python memory.
    The request's surrounding timeout also bounds slow/stalled body reads.
    """
    body = getattr(response, 'body', None)
    if body is None:
        raise ProviderError('invalid_provider_response')
    reader = body.getReader()
    data, complete = bytearray(), False
    try:
        while True:
            part = await reader.read()
            if part.done:
                complete = True
                break
            chunk = part.value
            native = isinstance(chunk, (bytes, bytearray))
            size = len(chunk) if native else chunk.byteLength
            if type(size) is not int or size < 0 or size > MAX_RESPONSE_BYTES - len(data):
                raise ProviderError('invalid_provider_response')
            data.extend(chunk if native else chunk.to_bytes())
    finally:
        try:
            if not complete:
                await reader.cancel()
        except Exception:
            pass
        try:
            reader.releaseLock()
        except Exception:
            pass
    try:
        value = json.loads(data.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise ProviderError('invalid_provider_response') from None
    if not isinstance(value, dict):
        raise ProviderError('invalid_provider_response')
    return value


class CloudProvider:
    def __init__(self, app_id, certificate, *, deepseek_api_key, fish_audio_api_key, fetcher=None):
        validate_credentials(app_id, certificate)
        if any(not isinstance(value, str) or not value.strip()
               for value in (deepseek_api_key, fish_audio_api_key)):
            raise ProviderError('missing_provider_configuration')
        self.app_id, self._certificate = app_id, certificate
        self._deepseek_key, self._fish_key = deepseek_api_key.strip(), fish_audio_api_key.strip()
        self._fetch = fetcher or _workers_fetch
        self._base = BASE_URL + app_id

    async def close(self):
        pass  # Fetch has no process-wide client pool to close.

    @staticmethod
    def _agent_path(agent_id):
        if not isinstance(agent_id, str) or not 1 <= len(agent_id) <= 128:
            raise ProviderError('invalid_agent_identifier')
        return '/agents/' + quote(agent_id, safe='')

    async def _request(self, method, path, *, channel, uid=AGENT_UID, payload=None,
                       timeout=4, allow_404=False, expect_json=True):
        token = convo_token(self.app_id, self._certificate, channel, uid, 60)
        options = {'method': method, 'headers': {
            'Authorization': 'agora token=' + token,
            'Accept': 'application/json', 'Cache-Control': 'no-store'},
            # Workers supports manual/follow, not redirect='error'. Manual
            # prevents credentials being forwarded; 3xx is rejected below.
            'redirect': 'manual', 'timeout': timeout}
        if payload is not None:
            options['headers']['Content-Type'] = 'application/json'
            options['body'] = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
        async def perform():
            response = await self._fetch(self._base + path, **options)
            async def discard():
                # Release the subrequest connection without reading private
                # error bodies; Workers limits simultaneous open connections.
                body = getattr(response, 'body', None)
                if body is not None:
                    await body.cancel()
            status = response.status
            if type(status) is not int:
                raise ProviderError('invalid_provider_response')
            if status == 404 and allow_404:
                await discard()
                return None
            if not 200 <= status < 300:
                # Do not read error bodies; they may contain secrets or text.
                await discard()
                raise ProviderError('provider_http_error', status)
            if not expect_json:
                await discard()
                return None
            return await _bounded_json(response)
        try:
            return await asyncio.wait_for(perform(), timeout=timeout)
        except ProviderError:
            raise
        except asyncio.TimeoutError:
            raise ProviderError('provider_timeout') from None
        except Exception:
            raise ProviderError('provider_unavailable') from None

    def _join_payload(self, channel, name, ttl):
        if type(ttl) is not int or not 1 <= ttl <= 600:
            raise ProviderError('invalid_session_expiry')
        return {'name': name, 'preset': 'deepgram_nova_3', 'pipeline_id': None, 'properties': {
            'channel': channel,
            'token': convo_token(self.app_id, self._certificate, channel, AGENT_UID, ttl),
            'agent_rtc_uid': AGENT_UID, 'remote_rtc_uids': [DEVICE_UID],
            'enable_string_uid': True, 'idle_timeout': 30,
            'advanced_features': {'enable_rtm': True},
            'tts': {'vendor': 'fishaudio', 'params': {
                'api_key': self._fish_key, 'reference_id': 'c534e7a4e96741ac97f253d0294df468',
                'backend': 's2-pro'}},
            'llm': {
                'url': 'https://api.deepseek.com/chat/completions',
                'params': {'model': 'deepseek-flash', 'thinking': {'type': 'disabled'}, 'max_tokens': 160},
                'style': 'openai', 'input_modalities': ['text'], 'api_key': self._deepseek_key,
                'greeting_message': GREETING, 'failure_message': FAILURE_MESSAGE, 'max_history': 15,
                'vendor': 'custom', 'system_messages': [{'role': 'system', 'content': PROMPT}],
                'greeting_configs': {'interruptable': False}},
            'turn_detection': {'language': STT_LANGUAGE, 'config': {
                'speech_threshold': 0.5,
                'start_of_speech': {'mode': 'vad', 'vad_config': {
                    'interrupt_duration_ms': 160, 'prefix_padding_ms': 300}},
                'end_of_speech': {'mode': 'vad', 'vad_config': {'silence_duration_ms': 480}}}},
            'parameters': {'data_channel': 'rtm', 'enable_metrics': True,
                           'enable_error_message': True, 'audio_scenario': 'aiserver'},
            # SDK 2.12.0 resolves managed nova-3 to the top-level preset and
            # removes params.model; preserve that exact managed-provider route.
            'asr': {'vendor': 'deepgram', 'params': {'language': STT_LANGUAGE}, 'language': STT_LANGUAGE},
        }}

    async def start(self, channel, name, ttl):
        result = await self._request('POST', '/join', channel=channel,
                                     payload=self._join_payload(channel, name, ttl), timeout=10)
        agent_id = result.get('agent_id')
        self._agent_path(agent_id)
        return agent_id

    async def stop(self, agent_id):
        # This channel/UID follows AsyncAgora.stop_agent's app-credentials path;
        # request auth is limited to 60 s rather than its default 24 hours.
        await self._request('POST', self._agent_path(agent_id) + '/leave',
            channel='stop', uid='0', allow_404=True, expect_json=False)

    async def speak(self, agent_id, channel, text):
        if not isinstance(text, str) or not 0 < len(text.encode('utf-8')) <= 512:
            raise ValueError('invalid_announcement')
        await self._request('POST', self._agent_path(agent_id) + '/speak', channel=channel,
            payload={'text': text, 'priority': 'INTERRUPT', 'interruptable': False}, expect_json=False)

    async def history(self, agent_id, channel):
        result = await self._request('GET', self._agent_path(agent_id) + '/history', channel=channel)
        contents = result.get('contents')
        if not isinstance(contents, list):
            raise ProviderError('invalid_provider_response')
        clean = []
        for item in contents:
            if not isinstance(item, dict) or item.get('role') not in ('user', 'assistant'):
                continue
            row = {'role': item['role'], 'content': item.get('content') if isinstance(item.get('content'), str) else None}
            for key in ('speech_start_ms', 'speech_end_ms'):
                value = item.get(key)
                row[key] = value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
            clean.append(row)
        history = {'contents': clean}
        # Official history/list examples use epoch seconds, never speech ms.
        # The shared cursor additionally validates this against its join/cutoff.
        start_ts = result.get('start_ts')
        if type(start_ts) is int and 1e9 <= start_ts < 1e11:
            history['start_ts'] = start_ts
        return history

    async def confirm_stopped(self, agent_id, channel):
        result = await self._request('GET', self._agent_path(agent_id), channel=channel, allow_404=True)
        return result is None or result.get('status') in ('STOPPED', 'FAILED')

    async def recover(self, channel):
        cursor, seen = '', set()
        for _ in range(3):
            query = {'channel': channel, 'limit': 20}
            if cursor:
                query['cursor'] = cursor
            result = await self._request('GET', '/agents?' + urlencode(query), channel=channel)
            data, meta = result.get('data'), result.get('meta')
            if not isinstance(data, dict) or not isinstance(data.get('list'), list) or not isinstance(meta, dict):
                raise ProviderError('invalid_provider_response')
            for row in data['list']:
                if not isinstance(row, dict):
                    raise ProviderError('invalid_provider_response')
                await self.stop(row.get('agent_id'))
            cursor = meta.get('cursor')
            if cursor in (None, ''):
                return
            if not isinstance(cursor, str) or len(cursor) > 256 or cursor in seen:
                raise ProviderError('invalid_provider_response')
            seen.add(cursor)
        raise ProviderError('recovery_incomplete')
