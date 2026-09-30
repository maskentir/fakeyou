from types import SimpleNamespace
from typing import cast

import httpx2
import pytest
from openai import Omit
from pipecat.services.meta.stt import MetaSTTService
from pipecat.transcriptions.language import Language

from fakeyou.voice import (
    MINIMAX_TTS_MODEL,
    MINIMAX_TTS_VOICE,
    TTS_HTTP_TIMEOUT,
    TTS_MODEL,
    TTS_REQUEST_TIMEOUT_SECONDS,
    TTS_VOICE,
    TTS_VOICE_INSTRUCTIONS,
    MiniMaxSpeechSynthesizer,
    MuseMode,
    OpenAISpeechSynthesizer,
    create_muse_stt,
    create_speech_synthesizer,
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


def _minimax_client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _sse(*events: dict) -> bytes:
    import json

    return b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)


async def test_minimax_synthesizer_sends_indonesian_pcm_request() -> None:
    import json

    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200, json={"data": {"audio": "0001ff7f", "status": 2}, "base_resp": {"status_code": 0}}
        )

    synthesizer = MiniMaxSpeechSynthesizer(api_key="minimax-key", client=_minimax_client(handler))

    audio = await synthesizer.synthesize("Hello.")

    assert audio == bytes.fromhex("0001ff7f")
    [request] = requests
    assert request.headers["authorization"] == "Bearer minimax-key"
    assert json.loads(request.content) == {
        "model": MINIMAX_TTS_MODEL,
        "text": "Hello.",
        "stream": False,
        "language_boost": "Indonesian",
        "voice_setting": {"voice_id": MINIMAX_TTS_VOICE},
        "audio_setting": {"sample_rate": 24000, "format": "pcm", "channel": 1},
    }


async def test_minimax_stream_yields_chunks_and_skips_the_aggregated_final_event() -> None:
    import json

    from fakeyou.journal import ProviderJournal

    bodies: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            content=_sse(
                {"data": {"audio": "0001", "status": 1}, "base_resp": {"status_code": 0}},
                {"data": {"audio": "0203", "status": 1}, "base_resp": {"status_code": 0}},
                {
                    "data": {"audio": "00010203", "status": 2},
                    "extra_info": {"audio_length": 1},
                    "base_resp": {"status_code": 0},
                },
            ),
        )

    lines: list[str] = []
    synthesizer = MiniMaxSpeechSynthesizer(
        api_key="k",
        client=_minimax_client(handler),
        journal=ProviderJournal("meeting-1", emit=lines.append),
    )

    chunks = [chunk async for chunk in synthesizer.stream("Hello.")]

    assert chunks == [b"\x00\x01", b"\x02\x03"]
    assert bodies[0]["stream"] is True
    assert bodies[0]["stream_options"] == {"exclude_aggregated_audio": True}
    finished = json.loads(lines[-1])
    assert (finished["event"], finished["bytes_out"], finished["streaming"]) == (
        "synth_finished",
        4,
        True,
    )


@pytest.mark.parametrize(
    ("status_code", "kind"),
    [(1002, "rate_limited"), (1004, "unauthorized"), (1008, "payment_required"), (9999, "unknown")],
)
async def test_minimax_error_status_raises_typed_provider_fault(status_code, kind) -> None:
    from fakeyou.provider_faults import ProviderFault

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, content=_sse({"base_resp": {"status_code": status_code, "status_msg": "error"}})
        )

    synthesizer = MiniMaxSpeechSynthesizer(api_key="k", client=_minimax_client(handler))

    with pytest.raises(ProviderFault) as caught:
        [chunk async for chunk in synthesizer.stream("Hello.")]

    assert (caught.value.source, caught.value.kind, caught.value.status) == (
        "tts",
        kind,
        status_code,
    )


async def test_minimax_http_error_raises_typed_provider_fault() -> None:
    from fakeyou.provider_faults import ProviderFault

    synthesizer = MiniMaxSpeechSynthesizer(
        api_key="k", client=_minimax_client(lambda request: httpx2.Response(503))
    )

    with pytest.raises(ProviderFault) as caught:
        await synthesizer.synthesize("Hello.")

    assert (caught.value.kind, caught.value.retryable) == ("unavailable", True)


async def test_minimax_synthesizer_closes_only_its_own_client() -> None:
    owned = MiniMaxSpeechSynthesizer(api_key="k")
    shared = _minimax_client(lambda request: httpx2.Response(200))
    borrowed = MiniMaxSpeechSynthesizer(api_key="k", client=shared)

    await owned.aclose()
    await borrowed.aclose()

    assert owned._client.is_closed
    assert not shared.is_closed
    await shared.aclose()


def test_create_speech_synthesizer_picks_provider_defaults() -> None:
    minimax = create_speech_synthesizer("minimax", api_key="k")
    openai = create_speech_synthesizer("openai", api_key="k", voice="nova")

    assert isinstance(minimax, MiniMaxSpeechSynthesizer)
    assert (minimax._model, minimax._voice) == (MINIMAX_TTS_MODEL, MINIMAX_TTS_VOICE)
    assert isinstance(openai, OpenAISpeechSynthesizer)
    assert (openai._model, openai._voice) == (TTS_MODEL, "nova")
