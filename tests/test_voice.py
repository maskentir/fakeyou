import json
from typing import cast

import httpx2
import pytest
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


async def test_synthesizer_sends_unary_pcm_request(openai_client) -> None:
    requests = []
    client = openai_client(requests=requests)
    synthesizer = OpenAISpeechSynthesizer(api_key="test-openai-key", client=client)

    try:
        audio = await synthesizer.synthesize("Hello.")
    finally:
        await synthesizer.aclose()

    assert audio == b"\x00\x01" * 100
    [request] = requests
    assert request.url == "https://api.openai.com/v1/audio/speech"
    assert request.headers["authorization"] == "Bearer test-openai-key"
    call = json.loads(request.content)
    assert call["model"] == TTS_MODEL
    assert call["voice"] == TTS_VOICE
    assert call["input"] == "Hello."
    assert call["instructions"] == TTS_VOICE_INSTRUCTIONS
    assert call["response_format"] == "pcm"
    assert "speed" not in call
    assert request.extensions["timeout"]["read"] == TTS_REQUEST_TIMEOUT_SECONDS


async def test_synthesizer_rejects_blank_text_before_request(openai_client) -> None:
    requests = []
    client = openai_client(requests=requests)
    synthesizer = OpenAISpeechSynthesizer(api_key="test-openai-key", client=client)

    try:
        with pytest.raises(ValueError, match="blank text"):
            await synthesizer.synthesize("   ")
    finally:
        await synthesizer.aclose()
    assert requests == []


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


async def test_synthesizer_timeout_raises_typed_provider_fault(openai_client) -> None:
    from fakeyou.provider_faults import ProviderFault

    synthesizer = OpenAISpeechSynthesizer(
        api_key="k", client=openai_client(error=httpx2.ReadTimeout("stalled"))
    )

    try:
        with pytest.raises(ProviderFault) as caught:
            await synthesizer.synthesize("Hello.")
    finally:
        await synthesizer.aclose()

    assert caught.value.source == "tts"
    assert caught.value.kind == "timeout"
    assert caught.value.retryable is True
    assert caught.value.__cause__ is not None


async def test_synthesizer_payment_status_raises_typed_provider_fault(openai_client) -> None:
    from fakeyou.provider_faults import ProviderFault

    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=openai_client(status=402))

    try:
        with pytest.raises(ProviderFault) as caught:
            await synthesizer.synthesize("Hello.")
    finally:
        await synthesizer.aclose()

    assert caught.value.source == "tts"
    assert caught.value.kind == "payment_required"
    assert caught.value.status == 402


async def test_synthesizer_journals_started_and_finished(journal, openai_client) -> None:

    lines = journal.lines
    requests = []
    client = openai_client(requests=requests)
    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=client, journal=journal)

    try:
        audio = await synthesizer.synthesize("Hello.")
    finally:
        await synthesizer.aclose()

    assert len(lines) == 2
    [started] = journal.events("synth_started")
    [finished] = journal.events("synth_finished")
    assert started["event"] == "synth_started"
    assert started["chars"] == len("Hello.")
    assert finished["event"] == "synth_finished"
    assert finished["duration_ms"] >= 0
    assert finished["chars"] == len("Hello.")
    assert finished["bytes_out"] == len(audio)


async def test_synthesizer_journals_failed_with_kind(journal, openai_client) -> None:

    from fakeyou.provider_faults import ProviderFault

    lines = journal.lines
    synthesizer = OpenAISpeechSynthesizer(
        api_key="k", client=openai_client(error=httpx2.ReadTimeout("stalled")), journal=journal
    )

    try:
        with pytest.raises(ProviderFault):
            await synthesizer.synthesize("Hello.")
    finally:
        await synthesizer.aclose()

    assert len(lines) == 2
    [failed] = journal.events("synth_failed")
    assert failed["event"] == "synth_failed"
    assert failed["kind"] == "timeout"
    assert failed["duration_ms"] >= 0


async def test_synthesizer_stream_yields_chunks_with_correlated_journal(
    journal, openai_client
) -> None:

    lines = journal.lines
    client = openai_client(chunks=[b"\x00" * 64, b"\x01" * 64])
    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=client, journal=journal)

    try:
        chunks = [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()

    assert chunks == [b"\x00" * 64, b"\x01" * 64]
    assert len(lines) == 2
    [started] = journal.events("synth_started")
    [finished] = journal.events("synth_finished")
    assert started["event"] == "synth_started"
    assert started["streaming"] is True
    assert finished["event"] == "synth_finished"
    assert finished["request_id"] == started["request_id"]
    assert finished["bytes_out"] == 128
    assert finished["streaming"] is True


async def test_synthesizer_stream_failure_raises_typed_fault(journal, openai_client) -> None:

    from fakeyou.provider_faults import ProviderFault

    lines = journal.lines
    client = openai_client(chunks=[b"\x00" * 64], stream_error=httpx2.ReadTimeout("stalled"))
    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=client, journal=journal)

    try:
        with pytest.raises(ProviderFault) as caught:
            [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()

    assert caught.value.source == "tts"
    assert caught.value.kind == "timeout"
    assert len(lines) == 2
    [failed] = journal.events("synth_failed")
    assert failed["event"] == "synth_failed"
    assert failed["request_id"] == journal.events()[0]["request_id"]


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


async def test_minimax_stream_yields_chunks_and_skips_the_aggregated_final_event(
    journal,
) -> None:
    import json

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

    synthesizer = MiniMaxSpeechSynthesizer(
        api_key="k",
        client=_minimax_client(handler),
        journal=journal,
    )

    chunks = [chunk async for chunk in synthesizer.stream("Hello.")]

    assert chunks == [b"\x00\x01", b"\x02\x03"]
    assert bodies[0]["stream"] is True
    assert bodies[0]["stream_options"] == {"exclude_aggregated_audio": True}
    [finished] = journal.events("synth_finished")
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
