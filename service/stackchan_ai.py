"""Bounded, single-device AI sessions for the touch-screen experiment.

Source map: original official quickstart server/src/agent.py supplies the local
provider builder; the portable builder below copies its proven US DeepSeek /
Deepgram / Fish configuration. Both use the same agentkit lifecycle, persona,
string UID mode and bounded sessions. No original HTTP app is imported.

The HTTP owner authenticates requests. Methods are thread safe and blocking;
SDK I/O runs on one private event loop, so stop can cancel an in-flight start.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from types import SimpleNamespace

from agent_profile import AGENT_UID, DEVICE_UID, FAILURE_MESSAGE, GREETING, PROMPT, STT_LANGUAGE


class SessionError(RuntimeError):
    """Safe, fixed errors only; upstream exception strings must not reach HTTP."""
    def __init__(self, code, status=503):
        super().__init__(code)
        self.code, self.status = code, status


def _portable_vendors(settings):
    """US provider settings copied from the working quickstart, SDK 2.12.0.

    Cloud startup requires both BYOK keys; a missing secret must not silently
    select the quickstart's different managed LLM or TTS fallback.
    """
    from agora_agent import CustomLLM
    from agora_agent.agentkit.vendors import DeepgramSTT, FishAudioTTS
    llm = CustomLLM(
        api_key=settings.deepseek_api_key, model='deepseek-flash',
        base_url='https://api.deepseek.com/chat/completions',
        greeting_message=settings.greeting, failure_message=FAILURE_MESSAGE,
        max_history=15, max_tokens=160,
        params={'thinking': {'type': 'disabled'}})
    stt = DeepgramSTT(model='nova-3', language=STT_LANGUAGE)
    tts = FishAudioTTS(
        key=settings.fish_audio_api_key,
        reference_id='c534e7a4e96741ac97f253d0294df468', backend='s2-pro')
    turn_detection = {'config': {
        'speech_threshold': 0.5,
        'start_of_speech': {'mode': 'vad', 'vad_config': {
            'interrupt_duration_ms': 160, 'prefix_padding_ms': 300}},
        'end_of_speech': {'mode': 'vad', 'vad_config': {'silence_duration_ms': 480}},
    }}
    return llm, stt, tts, turn_detection


class QuickstartProvider:
    @classmethod
    def from_environment(cls, app_id, certificate, environment=None):
        """Build without the original checkout or .env file.

        Only AGORA_AREA, DEEPSEEK_API_KEY and FISH_AUDIO_API_KEY are read from
        the supplied mapping (os.environ when omitted). Credentials stay in
        memory; the HTTP owner must disable raw SDK logging as in ai_server.
        """
        return cls(None, app_id, certificate,
                   environment=os.environ if environment is None else environment)

    def __init__(self, original_server, app_id, certificate, *, environment=None):
        # Import definitions only. Do not import src/server.py (starts a global
        # client and applies the old service's environment and HTTP defaults).
        from agora_agent import Area, AsyncAgora
        import httpx
        from agora_agent.agentkit import Agent
        self._agent_class = Agent
        self._app_id, self._certificate = app_id, certificate
        if original_server is None:
            env = os.environ if environment is None else environment
            raw_area = env.get('AGORA_AREA') or 'us'
            if (not isinstance(raw_area, str) or raw_area.strip().lower() not in
                    ('us', 'usa', 'global', 'intl', 'international')):
                raise SessionError('unsupported_region')
            area = 'us'
            for key, error in (('DEEPSEEK_API_KEY', 'missing_deepseek_api_key'),
                               ('FISH_AUDIO_API_KEY', 'missing_fish_audio_api_key')):
                value = env.get(key)
                if not isinstance(value, str) or not value.strip():
                    raise SessionError(error)
            self._vendors = _portable_vendors
        else:
            if environment is not None:
                raise ValueError('ambiguous_provider_configuration')
            from dotenv import dotenv_values
            original_server = Path(original_server)
            spec = importlib.util.spec_from_file_location(
                'stackchan_existing_quickstart_agent', original_server / 'src/agent.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            env = dotenv_values(original_server / '.env')
            area = module._normalize_area(env.get('AGORA_AREA'))
            self._vendors = module.Agent._build_vendors
        self._settings = SimpleNamespace(
            area_name=area, greeting=GREETING,
            minimax_api_key=env.get('MINIMAX_API_KEY') if original_server is not None else None,
            deepseek_api_key=(env.get('DEEPSEEK_API_KEY') or '').strip(),
            fish_audio_api_key=(env.get('FISH_AUDIO_API_KEY') or '').strip())
        # Keep the proven US WHIP/SDK region aligned. CN requires a different
        # WHIP endpoint and therefore isn't silently selected in this prototype.
        if area != 'us':
            raise SessionError('unsupported_region')
        self._http = httpx.AsyncClient(timeout=10)
        self._client = AsyncAgora(area=Area.US, app_id=app_id,
                                  app_certificate=certificate, timeout=10,
                                  httpx_client=self._http, debug=False)

    async def close(self):
        await self._http.aclose()

    async def start(self, channel, name, ttl):
        llm, stt, tts, turn_detection = self._vendors(self._settings)
        # Keep the user's selected recognition language explicit and shared
        # with the local diagnostic page. The model and existing voice stay fixed.
        stt.language = STT_LANGUAGE
        if hasattr(tts, 'language_boost'):
            tts.language_boost = 'English'
        if hasattr(llm, 'failure_message'):
            llm.failure_message = FAILURE_MESSAGE
        agent = self._agent_class(
            client=self._client, instructions=PROMPT, greeting=GREETING,
            # Startup noise must not truncate the welcome message. Normal
            # conversation interruption/VAD settings remain unchanged.
            greeting_configs={'interruptable': False},
            failure_message=FAILURE_MESSAGE, max_history=15,
            turn_detection=turn_detection,
            advanced_features={'enable_rtm': True},
            # Agora's default AI scenario balances playout and network jitter;
            # chorus is the specialized ultra-low-latency music configuration.
            parameters={'audio_scenario': 'aiserver', 'data_channel': 'rtm',
                        'enable_error_message': True, 'enable_metrics': True})
        agent = agent.with_stt(stt).with_llm(llm).with_tts(tts)
        session = agent.create_async_session(
            channel=channel, name=name, agent_uid=AGENT_UID,
            remote_uids=[DEVICE_UID], enable_string_uid=True,
            idle_timeout=30, expires_in=ttl, debug=False)
        agent_id = await session.start()
        if not isinstance(agent_id, str) or not agent_id:
            raise SessionError('agent_start_failed')
        return agent_id

    async def stop(self, agent_id):
        # SDK stop_agent treats an already-stopped agent (HTTP 404) as success.
        await self._client.stop_agent(agent_id)

    def _request_options(self, channel):
        from agora_agent.agentkit.token import generate_convo_ai_token
        token = generate_convo_ai_token(self._app_id, self._certificate,
                                       channel, int(AGENT_UID), token_expire=60)
        return {'additional_headers': {'Authorization': 'agora token=' + token},
                'max_retries': 0, 'timeout_in_seconds': 4}

    async def speak(self, agent_id, channel, text):
        if not isinstance(text, str) or not 0 < len(text.encode('utf-8')) <= 512:
            raise ValueError('invalid_announcement')
        await self._client.agents.speak(self._app_id, agent_id, text=text,
            priority='INTERRUPT', interruptable=False,
            request_options=self._request_options(channel))

    async def history(self, agent_id, channel):
        return await self._client.agents.get_history(self._app_id, agent_id,
            request_options=self._request_options(channel))

    async def confirm_stopped(self, agent_id, channel):
        from agora_agent.core.api_error import ApiError
        try:
            result = await self._client.agents.get(self._app_id, agent_id,
                request_options=self._request_options(channel))
        except ApiError as exc:
            if exc.status_code == 404:
                return True
            raise
        return getattr(result, 'status', None) in ('STOPPED', 'FAILED')

    async def recover(self, channel):
        """Stop only agents in our journal's unique channel after a lost reply."""
        from agora_agent.agentkit.token import generate_convo_ai_token
        token = generate_convo_ai_token(self._app_id, self._certificate,
                                       channel, int(AGENT_UID), token_expire=60)
        pager = await self._client.agents.list(
            self._app_id, channel=channel, limit=20,
            request_options={'additional_headers': {'Authorization': f'agora token={token}'},
                             'max_retries': 0, 'timeout_in_seconds': 10})
        async for agent in pager:
            agent_id = getattr(agent, 'agent_id', None)
            if agent_id:
                await self.stop(agent_id)


class FileJournal:
    """Local implementation of the synchronous read/write/clear journal contract.

    read returns a recovery row or None; write commits before returning; clear
    is idempotent. Runtime ownership/locking stays with Manager, not storage.
    """
    def __init__(self, path):
        self.path = Path(path)

    def read(self):
        try:
            row = json.loads(self.path.read_text())
            if row is None:
                raise ValueError('invalid_recovery_journal')
            return row
        except FileNotFoundError:
            return None

    def write(self, row):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(row, output, separators=(',', ':'))
            output.flush()
            os.fsync(output.fileno())
        tmp.replace(self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)


class Manager:
    def __init__(self, provider, token_factory, journal_path, *,
                 max_seconds=600, lease_seconds=45, clock=time.time):
        import fcntl  # Local process ownership; Workers use a Durable Object.
        journal = FileJournal(journal_path)
        self._init_state(provider, token_factory, journal, max_seconds=max_seconds,
                         lease_seconds=lease_seconds, clock=clock)
        journal.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = journal.path.with_suffix('.lock').open('a')
        os.chmod(self._lock_file.name, 0o600)
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock_file.close()
            raise SessionError('service_already_running', 409) from None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever,
                                        name='stackchan-ai', daemon=True)
        self._thread.start()
        try:
            self._call(self._initialize())
        except Exception:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(2)
            self._loop.close()
            self._lock_file.close()
            raise

    def _init_state(self, provider, token_factory, journal, *, max_seconds=600,
                    lease_seconds=45, clock=time.time, recent=None):
        """Shared state only: no threads, tasks, storage reads or provider I/O."""
        if not 30 <= max_seconds <= 600 or not 10 <= lease_seconds <= 60:
            raise ValueError('Invalid session bounds')
        self.provider, self.token_factory, self.journal = provider, token_factory, journal
        self.max_seconds, self.lease_seconds = max_seconds, lease_seconds
        self.clock = clock
        self._record = None
        self._closed = False
        self._recent = dict(recent) if recent is not None else {}
        self.start_guard = None  # Optional call owner; invoked on its event loop.

    @staticmethod
    def _identifier(value):
        return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9_-]{8,64}', value))

    def _call(self, coroutine, timeout=25):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            # Do not cancel the underlying join: its eventual agent ID is needed
            # for cleanup even if the HTTP response has already been lost.
            raise SessionError('operation_pending', 503) from None

    def start(self, request_id):
        if not self._identifier(request_id):
            raise SessionError('invalid_request_id', 400)
        try:
            return self._call(self._start(request_id))
        except SessionError as exc:
            if exc.code == 'operation_pending':
                self.stop(request_id=request_id)
            raise

    def heartbeat(self, session_id):
        if not self._identifier(session_id):
            raise SessionError('invalid_session_id', 400)
        return self._call(self._heartbeat(session_id), timeout=2)

    def stop(self, session_id=None, request_id=None):
        if (session_id is None) == (request_id is None):
            raise SessionError('one_session_identifier_required', 400)
        value = session_id if session_id is not None else request_id
        if not self._identifier(value):
            raise SessionError('invalid_session_identifier', 400)
        return self._call(self._stop(session_id, request_id))

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._call(self._shutdown(), timeout=50)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(2)
            # A pending cloud call retains its journal for recovery on restart.
            if not self._thread.is_alive():
                self._loop.close()
            self._lock_file.close()

    def _save(self):
        fields = ('request_id', 'session_id', 'channel', 'agent_id', 'expires_at', 'created_at')
        self.journal.write({key: self._record[key] for key in fields})

    def _restore(self):
        """Validate storage before admitting work; both runtimes recover alike."""
        try:
            row = self.journal.read()
            if row is None:
                return
            if (not isinstance(row, dict)
                or not isinstance(row.get('channel'), str)
                or not re.fullmatch(r'stackchan-ai-[a-f0-9]{32}', row['channel'])
                or not self._identifier(row.get('request_id'))
                or not self._identifier(row.get('session_id'))
                or type(row.get('expires_at')) is not int
                or type(row.get('created_at')) is not int
                or not isinstance(row.get('agent_id'), str)):
                raise ValueError()
            # Expired credentials never authorize discarding recovery work.
            self._record = dict(row, state='cleanup', cancel=True, task=None,
                                cleanup_after=self.clock() + (35 if not row['agent_id'] else 0),
                                lease_until=0)
        except (ValueError, TypeError, OSError):
            raise SessionError('invalid_recovery_journal') from None

    async def _initialize(self):
        self._restore()
        self._watchdog = asyncio.create_task(self._watch())
        if self._record:
            await self._cleanup(self._record)

    def _result(self, record):
        return {'session_id': record['session_id'], 'server_time': int(self.clock()),
                'whip_url': f"https://ap-webrtc-whip.ap.sd-rtn.com/pub/{record['channel']}?stringuid={DEVICE_UID}&duplex=true",
                'whip_token': self.token_factory(record['channel'], record['expires_at']),
                'expires_at': record['expires_at'], 'lease_seconds': self.lease_seconds}

    async def _start(self, request_id):
        if self._closed:
            raise SessionError('service_stopping')
        if self.start_guard is not None and not self.start_guard():
            raise SessionError('human_call_in_progress', 409)
        now = self.clock()
        self._recent = {key: until for key, until in self._recent.items() if until > now}
        if request_id in self._recent:
            raise SessionError('request_already_stopped', 409)
        if self._record:
            rec = self._record
            if rec['request_id'] != request_id or rec['cancel']:
                raise SessionError('session_busy', 409)
            if rec['state'] == 'active':
                if now >= min(rec['expires_at'], rec['lease_until']):
                    rec['cancel'] = True
                    await self._cleanup(rec)
                    raise SessionError('session_expired', 410)
                return self._result(rec)
            return await asyncio.shield(rec['task'])
        rec = {'request_id': request_id, 'session_id': secrets.token_hex(16),
               'channel': 'stackchan-ai-' + secrets.token_hex(16),
               'agent_id': '', 'created_at': int(now), 'expires_at': int(now) + self.max_seconds,
               'state': 'starting', 'cancel': False, 'lease_until': now + self.lease_seconds,
               'cleanup_after': now + 35, 'task': None}
        self._record = rec
        self._save()  # Durable intent before the cloud request.
        rec['task'] = asyncio.create_task(self._create(rec))
        return await asyncio.shield(rec['task'])

    async def _create(self, rec):
        try:
            rec['agent_id'] = await asyncio.wait_for(self.provider.start(rec['channel'],
                'stackchan-' + rec['session_id'], self.max_seconds), timeout=20)
            self._save()
        except Exception:
            rec['cancel'], rec['state'] = True, 'cleanup'
            rec['cleanup_after'] = self.clock() + 35
            await self._cleanup(rec)
            raise SessionError('agent_start_failed') from None
        if rec['cancel'] or self._closed or self.clock() >= rec['expires_at']:
            await self._cleanup(rec)
            raise SessionError('start_canceled', 409)
        rec['state'] = 'active'
        rec['lease_until'] = self.clock() + self.lease_seconds
        return self._result(rec)

    async def _heartbeat(self, session_id):
        rec = self._record
        if not rec or rec['session_id'] != session_id or rec['state'] != 'active' or rec['cancel']:
            raise SessionError('session_not_active', 410)
        if self.clock() >= min(rec['expires_at'], rec['lease_until']):
            rec['cancel'] = True
            asyncio.create_task(self._cleanup(rec))
            raise SessionError('session_expired', 410)
        rec['lease_until'] = self.clock() + self.lease_seconds
        return {'active': True, 'state': 'active', 'expires_at': rec['expires_at'], 'lease_seconds': self.lease_seconds}

    async def _stop(self, session_id, request_id):
        rec = self._record
        if not rec or (session_id is not None and rec['session_id'] != session_id) or (
                request_id is not None and rec['request_id'] != request_id):
            if request_id:
                self._recent[request_id] = self.clock() + self.max_seconds
            return {'active': False, 'state': 'stopped'}
        rec['cancel'] = True
        if rec['state'] == 'starting':
            return {'active': False, 'state': 'stopping'}
        await self._cleanup(rec)
        if self._record is rec:
            raise SessionError('cleanup_pending')
        return {'active': False, 'state': 'stopped'}

    async def _cleanup(self, rec):
        if self._record is not rec or rec['state'] == 'stopping':
            return
        rec['state'], rec['cancel'] = 'stopping', True
        try:
            if rec['agent_id']:
                await asyncio.wait_for(self.provider.stop(rec['agent_id']), timeout=12)
            else:
                await asyncio.wait_for(self.provider.recover(rec['channel']), timeout=12)
                # A join whose reply was lost may still be propagating. Retain
                # the tombstone and retry before admitting another session.
                if self.clock() < rec['cleanup_after']:
                    rec['state'] = 'cleanup'
                    return
        except asyncio.CancelledError:
            rec['state'] = 'cleanup'
            raise
        except Exception:
            rec['state'] = 'cleanup'
            return
        self._recent[rec['request_id']] = self.clock() + self.max_seconds
        self.journal.clear()
        self._record = None

    async def _watch(self):
        while True:
            await asyncio.sleep(1)
            rec = self._record
            if not rec:
                continue
            if rec['state'] == 'active' and self.clock() >= min(rec['lease_until'], rec['expires_at']):
                rec['cancel'] = True
            if rec['state'] == 'cleanup' or (rec['state'] == 'active' and rec['cancel']):
                await self._cleanup(rec)

    async def _shutdown(self):
        self._watchdog.cancel()
        await asyncio.gather(self._watchdog, return_exceptions=True)
        rec = self._record
        if rec:
            rec['cancel'] = True
            if rec['state'] == 'starting':
                try:
                    await asyncio.shield(rec['task'])
                except (Exception, asyncio.CancelledError):
                    pass
            if self._record is rec and rec['state'] != 'starting':
                await self._cleanup(rec)

        if hasattr(self.provider, 'close'):
            await self.provider.close()
