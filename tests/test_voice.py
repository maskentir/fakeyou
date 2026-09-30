from types import SimpleNamespace
from typing import cast

import httpx2
import pytest
from openai import Omit
from pipecat.services.meta.stt import MetaSTTService
from pipecat.transcriptions.language import Language

from fakeyou.voice import (
    TTS_HTTP_TIMEOUT,
    TTS_MODEL,
    TTS_REQUEST_TIMEOUT_SECONDS,
    TTS_VOICE,
    TTS_VOICE_INSTRUCTIONS,
    MuseMode,
    OpenAISpeechSynthesizer,
    create_muse_stt,
    create_tts_http_client,
)


def test_create_muse_stt_builds_native_indonesian_endpointing_service() -> None:
    service = create_muse_stt(
        api_key="test-meta-key",
        language=Language.ID,
        keywords=["Jemboed"],
    )

    assert isinstance(service, MetaSTTService)
    assert service._settings.language == "Indonesian"
    assert service._settings.mode == "ENDPOINTING"
    assert service._settings.keywords == ["Jemboed"]
    assert service._settings.emit_audio_progress is False


def test_create_muse_stt_rejects_unknown_mode_before_connecting() -> None:
    with pytest.raises(ValueError, match="unsupported Muse mode"):
        create_muse_stt(api_key="test-meta-key", mode=cast(MuseMode, "UNKNOWN"))


async def test_tts_http_client_bounds_slow_streams() -> None:
    client = create_tts_http_client()
    try:
        assert client.timeout == httpx2.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        assert client.timeout == TTS_HTTP_TIMEOUT
    finally:
        await client.aclose()
    assert client.is_closed


class _FakeSpeech:
    def __init__(self, audio: bytes = b"\x00\x01" * 100) -> None:
        self.audio = audio
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(read=lambda: self.audio)


def _fake_client(speech: _FakeSpeech):
    return SimpleNamespace(audio=SimpleNamespace(speech=speech))


async def test_synthesizer_sends_unary_pcm_request() -> None:
    speech = _FakeSpeech()
    synthesizer = OpenAISpeechSynthesizer(api_key="test-openai-key", client=_fake_client(speech))

    try:
        audio = await synthesizer.synthesize("Hello.")
    finally:
        await synthesizer.aclose()

    assert audio == speech.audio
    assert len(speech.calls) == 1
    call = speech.calls[0]
    assert call["model"] == TTS_MODEL
    assert call["voice"] == TTS_VOICE
    assert call["input"] == "Hello."
    assert call["instructions"] == TTS_VOICE_INSTRUCTIONS
    assert call["response_format"] == "pcm"
    assert isinstance(call["speed"], Omit)
    assert call["timeout"] == TTS_REQUEST_TIMEOUT_SECONDS


async def test_synthesizer_rejects_blank_text_before_request() -> None:
    speech = _FakeSpeech()
    synthesizer = OpenAISpeechSynthesizer(api_key="test-openai-key", client=_fake_client(speech))

    try:
        with pytest.raises(ValueError, match="blank text"):
            await synthesizer.synthesize("   ")
    finally:
        await synthesizer.aclose()
    assert speech.calls == []


def test_synthesizer_rejects_unknown_voice_before_connecting() -> None:
    with pytest.raises(ValueError, match="unsupported OpenAI voice"):
        OpenAISpeechSynthesizer(api_key="test-openai-key", voice="robot")


async def test_synthesizer_closes_owned_http_client() -> None:
    synthesizer = OpenAISpeechSynthesizer(api_key="test-openai-key")
    owned = synthesizer._http_client

    assert owned is not None
    assert not owned.is_closed
    await synthesizer.aclose()
    await synthesizer.aclose()
    assert owned.is_closed


def test_synthesizer_disables_sdk_retries() -> None:
    synthesizer = OpenAISpeechSynthesizer(api_key="test-openai-key")
    assert synthesizer._client.max_retries == 0


def _timeout_client():
    import openai

    speech = _FakeSpeech()

    async def create(**kwargs):
        raise openai.APITimeoutError(httpx2.Request("POST", "https://api.openai.com/x"))

    speech.create = create
    return _fake_client(speech)


def _status_client(status: int):
    import openai

    speech = _FakeSpeech()

    async def create(**kwargs):
        request = httpx2.Request("POST", "https://api.openai.com/x")
        response = httpx2.Response(status, request=request)
        raise openai.APIStatusError(f"status {status}", response=response, body=None)

    speech.create = create
    return _fake_client(speech)


async def test_synthesizer_timeout_raises_typed_provider_fault() -> None:
    from fakeyou.provider_faults import ProviderFault

    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=_timeout_client())

    try:
        with pytest.raises(ProviderFault) as caught:
            await synthesizer.synthesize("Hello.")
    finally:
        await synthesizer.aclose()

    assert caught.value.source == "tts"
    assert caught.value.kind == "timeout"
    assert caught.value.retryable is True
    assert caught.value.__cause__ is not None


async def test_synthesizer_payment_status_raises_typed_provider_fault() -> None:
    from fakeyou.provider_faults import ProviderFault

    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=_status_client(402))

    try:
        with pytest.raises(ProviderFault) as caught:
            await synthesizer.synthesize("Hello.")
    finally:
        await synthesizer.aclose()

    assert caught.value.source == "tts"
    assert caught.value.kind == "payment_required"
    assert caught.value.status == 402


async def test_synthesizer_journals_started_and_finished() -> None:
    import json

    from fakeyou.journal import ProviderJournal

    lines: list[str] = []
    journal = ProviderJournal("meeting-1", emit=lines.append)
    speech = _FakeSpeech()
    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=_fake_client(speech), journal=journal)

    try:
        audio = await synthesizer.synthesize("Hello.")
    finally:
        await synthesizer.aclose()

    assert len(lines) == 2
    started = json.loads(lines[0])
    finished = json.loads(lines[1])
    assert started["event"] == "synth_started"
    assert started["chars"] == len("Hello.")
    assert finished["event"] == "synth_finished"
    assert finished["duration_ms"] >= 0
    assert finished["chars"] == len("Hello.")
    assert finished["bytes_out"] == len(audio)


async def test_synthesizer_journals_failed_with_kind() -> None:
    import json

    from fakeyou.journal import ProviderJournal
    from fakeyou.provider_faults import ProviderFault

    lines: list[str] = []
    journal = ProviderJournal("meeting-1", emit=lines.append)
    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=_timeout_client(), journal=journal)

    try:
        with pytest.raises(ProviderFault):
            await synthesizer.synthesize("Hello.")
    finally:
        await synthesizer.aclose()

    assert len(lines) == 2
    failed = json.loads(lines[1])
    assert failed["event"] == "synth_failed"
    assert failed["kind"] == "timeout"
    assert failed["duration_ms"] >= 0


class _FakeStreamResponse:
    def __init__(self, chunks, error: Exception | None = None) -> None:
        self._chunks = list(chunks)
        self._error = error

    async def iter_bytes(self):
        for chunk in self._chunks:
            yield chunk
        if self._error is not None:
            raise self._error


class _FakeStreamContext:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._response

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _FakeStreamSpeech:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self.calls: list[dict] = []
        self.with_streaming_response = SimpleNamespace(create=self._create)
        self._response = response

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeStreamContext(self._response)


def _stream_client(response: _FakeStreamResponse):
    return SimpleNamespace(audio=SimpleNamespace(speech=_FakeStreamSpeech(response)))


async def test_synthesizer_stream_yields_chunks_with_correlated_journal() -> None:
    import json

    from fakeyou.journal import ProviderJournal

    lines: list[str] = []
    journal = ProviderJournal("meeting-1", emit=lines.append)
    response = _FakeStreamResponse([b"\x00" * 64, b"\x01" * 64])
    synthesizer = OpenAISpeechSynthesizer(
        api_key="k", client=_stream_client(response), journal=journal
    )

    try:
        chunks = [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()

    assert chunks == [b"\x00" * 64, b"\x01" * 64]
    assert len(lines) == 2
    started = json.loads(lines[0])
    finished = json.loads(lines[1])
    assert started["event"] == "synth_started"
    assert started["streaming"] is True
    assert finished["event"] == "synth_finished"
    assert finished["request_id"] == started["request_id"]
    assert finished["bytes_out"] == 128
    assert finished["streaming"] is True


async def test_synthesizer_stream_failure_raises_typed_fault() -> None:
    import json

    from fakeyou.journal import ProviderJournal
    from fakeyou.provider_faults import ProviderFault

    lines: list[str] = []
    journal = ProviderJournal("meeting-1", emit=lines.append)
    response = _FakeStreamResponse([b"\x00" * 64], error=TimeoutError("stalled"))
    synthesizer = OpenAISpeechSynthesizer(
        api_key="k", client=_stream_client(response), journal=journal
    )

    try:
        with pytest.raises(ProviderFault) as caught:
            [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()

    assert caught.value.source == "tts"
    assert caught.value.kind == "timeout"
    assert len(lines) == 2
    failed = json.loads(lines[1])
    assert failed["event"] == "synth_failed"
    assert failed["request_id"] == json.loads(lines[0])["request_id"]
