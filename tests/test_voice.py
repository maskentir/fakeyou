import json
from typing import cast

import httpx2
import pytest
from pipecat.services.meta.stt import MetaSTTService
from pipecat.transcriptions.language import Language

from fakeyou.invariants import PLAYBACK_SAMPLE_RATE
from fakeyou.settings import DEFAULTS, MiniMaxTTS, OpenAITTS, STTMode
from fakeyou.voice import (
    SPEECH_LANGUAGES,
    TTS_HTTP_TIMEOUT,
    TTS_REQUEST_TIMEOUT_SECONDS,
    MiniMaxSpeechSynthesizer,
    OpenAISpeechSynthesizer,
    create_muse_stt,
    create_speech_synthesizer,
    tts_voice_instructions,
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
        create_muse_stt(api_key="test-meta-key", mode=cast(STTMode, "UNKNOWN"))


async def test_tts_http_client_bounds_slow_streams() -> None:
    synthesizer = OpenAISpeechSynthesizer(api_key="test-key")
    client = synthesizer._client
    try:
        assert client.timeout == httpx2.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        assert client.timeout == TTS_HTTP_TIMEOUT
    finally:
        await synthesizer.aclose()
    assert client.is_closed()


async def test_synthesizer_streams_a_pcm_request(openai_client) -> None:
    requests = []
    client = openai_client(requests=requests)
    synthesizer = OpenAISpeechSynthesizer(api_key="test-openai-key", client=client)

    try:
        audio = b"".join([chunk async for chunk in synthesizer.stream("Hello.")])
    finally:
        await synthesizer.aclose()

    assert audio == b"\x00\x01" * 100
    [request] = requests
    assert request.url == "https://api.openai.com/v1/audio/speech"
    assert request.headers["authorization"] == "Bearer test-openai-key"
    call = json.loads(request.content)
    assert call["model"] == DEFAULTS.tts.model
    assert call["voice"] == DEFAULTS.tts.voice
    assert call["input"] == "Hello."
    assert call["instructions"] == tts_voice_instructions(DEFAULTS.speech.language)
    assert call["response_format"] == "pcm"
    assert call["stream_format"] == "audio"
    assert "speed" not in call
    assert request.extensions["timeout"]["read"] == TTS_REQUEST_TIMEOUT_SECONDS


async def test_synthesizer_rejects_blank_text_before_request(openai_client) -> None:
    requests = []
    client = openai_client(requests=requests)
    synthesizer = OpenAISpeechSynthesizer(api_key="test-openai-key", client=client)

    try:
        with pytest.raises(ValueError, match="blank text"):
            [chunk async for chunk in synthesizer.stream("   ")]
    finally:
        await synthesizer.aclose()
    assert requests == []


def test_synthesizer_rejects_unknown_voice_before_connecting() -> None:
    with pytest.raises(ValueError, match="unsupported OpenAI voice"):
        OpenAISpeechSynthesizer(api_key="test-openai-key", voice="robot")


async def test_synthesizer_closes_owned_http_client() -> None:
    synthesizer = OpenAISpeechSynthesizer(api_key="test-openai-key")
    owned = synthesizer._client

    assert owned is not None
    assert not owned.is_closed()
    await synthesizer.aclose()
    await synthesizer.aclose()
    assert owned.is_closed()


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
            [chunk async for chunk in synthesizer.stream("Hello.")]
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
            [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()

    assert caught.value.source == "tts"
    assert caught.value.kind == "payment_required"
    assert caught.value.status == 402


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
    assert len(lines) == 3
    [started] = journal.events("synthesis_started")
    [first] = journal.events("synthesis_pcm_emitted")
    [finished] = journal.events("synthesis_finished")
    assert started["event"] == "synthesis_started"
    assert started["streaming"] is True
    assert started["chars"] == len("Hello.")
    assert first["request_id"] == started["request_id"]
    assert first["bytes_out"] == 64
    assert finished["event"] == "synthesis_finished"
    assert finished["request_id"] == started["request_id"]
    assert finished["chars"] == len("Hello.")
    assert finished["bytes_out"] == 128
    assert finished["duration_ms"] >= 0
    assert finished["first_chunk_ms"] >= 0
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
    assert len(lines) == 3
    [first] = journal.events("synthesis_pcm_emitted")
    [failed] = journal.events("synthesis_failed")
    assert first["bytes_out"] == 64
    assert failed["event"] == "synthesis_failed"
    assert failed["kind"] == "timeout"
    assert failed["duration_ms"] >= 0
    assert failed["request_id"] == journal.events()[0]["request_id"]


def _minimax_client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _sse(*events: dict) -> bytes:
    import json

    return b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)


async def test_minimax_synthesizer_streams_an_indonesian_pcm_request() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200, content=_sse({"data": {"audio": "0001ff7f"}, "base_resp": {"status_code": 0}})
        )

    synthesizer = MiniMaxSpeechSynthesizer(api_key="minimax-key", client=_minimax_client(handler))

    audio = b"".join([chunk async for chunk in synthesizer.stream("Hello.")])

    assert audio == bytes.fromhex("0001ff7f")
    [request] = requests
    assert request.headers["authorization"] == "Bearer minimax-key"
    assert json.loads(request.content) == {
        "model": MiniMaxTTS().model,
        "text": "Hello.",
        "stream": True,
        "language_boost": SPEECH_LANGUAGES[DEFAULTS.speech.language][2],
        "voice_setting": {"voice_id": MiniMaxTTS().voice},
        "audio_setting": {"sample_rate": PLAYBACK_SAMPLE_RATE, "format": "pcm", "channel": 1},
        "stream_options": {"exclude_aggregated_audio": True},
    }


async def test_minimax_stream_yields_chunks_and_skips_the_aggregated_final_event(
    journal,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
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
    [finished] = journal.events("synthesis_finished")
    assert (finished["event"], finished["bytes_out"], finished["streaming"]) == (
        "synthesis_finished",
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
        [chunk async for chunk in synthesizer.stream("Hello.")]

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


async def test_first_pcm_is_reported_before_synthesis_finishes(openai_client, journal):
    synthesizer = OpenAISpeechSynthesizer(
        client=openai_client(chunks=[b"\x00\x01", b"\x02\x03"]), journal=journal
    )
    stream = synthesizer.stream("Hello.")
    try:
        assert await anext(stream) == b"\x00\x01"
        [first] = journal.events("synthesis_pcm_emitted")
        assert first["first_chunk_ms"] >= 0
        assert first["bytes_out"] == 2
        assert journal.events("synthesis_finished") == []
    finally:
        await stream.aclose()
        await synthesizer.aclose()


async def test_first_pcm_event_carries_correlation_without_content(openai_client, journal):
    synthesizer = OpenAISpeechSynthesizer(
        client=openai_client(chunks=[b"\x00\x01", b"\x02\x03"]), journal=journal
    )
    try:
        chunks = [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()
    assert chunks == [b"\x00\x01", b"\x02\x03"]
    [started] = journal.events("synthesis_started")
    [first] = journal.events("synthesis_pcm_emitted")
    [finished] = journal.events("synthesis_finished")
    started_request = started["request_id"]
    assert set(first) == {
        "event",
        "meeting_id",
        "at",
        "source",
        "request_id",
        "first_chunk_ms",
        "bytes_out",
    }
    assert first["source"] == "tts"
    assert first["request_id"] == started_request
    assert finished["request_id"] == started_request
    assert first["bytes_out"] == 2
    assert finished["bytes_out"] == 4
    assert finished["first_chunk_ms"] == first["first_chunk_ms"]


async def test_empty_chunks_are_skipped_before_first_pcm(openai_client, journal):
    synthesizer = OpenAISpeechSynthesizer(
        client=openai_client(chunks=[b"", b"\x00\x01", b""]), journal=journal
    )
    try:
        chunks = [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()
    assert chunks == [b"\x00\x01"]
    [first] = journal.events("synthesis_pcm_emitted")
    [finished] = journal.events("synthesis_finished")
    assert first["bytes_out"] == 2
    assert finished["bytes_out"] == 2
    assert finished["first_chunk_ms"] == first["first_chunk_ms"]


async def test_journaled_stream_skips_empty_chunks(journal):
    from fakeyou.voice import _journaled_stream

    async def open_stream(text):
        assert text == "Hello."
        yield b""
        yield b"\x00\x01"
        yield b""

    chunks = [chunk async for chunk in _journaled_stream(journal, "Hello.", open_stream)]

    assert chunks == [b"\x00\x01"]
    [first] = journal.events("synthesis_pcm_emitted")
    [finished] = journal.events("synthesis_finished")
    assert first["bytes_out"] == 2
    assert finished["bytes_out"] == 2


async def test_empty_stream_records_no_first_pcm(openai_client, journal):
    synthesizer = OpenAISpeechSynthesizer(client=openai_client(chunks=[b"", b""]), journal=journal)
    try:
        chunks = [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()
    assert chunks == []
    assert journal.events("synthesis_pcm_emitted") == []
    [finished] = journal.events("synthesis_finished")
    assert finished["bytes_out"] == 0
    assert finished["first_chunk_ms"] is None


async def test_failure_before_first_pcm_records_no_first_pcm(openai_client, journal):
    from fakeyou.provider_faults import ProviderFault

    synthesizer = OpenAISpeechSynthesizer(
        api_key="k",
        client=openai_client(chunks=[], stream_error=httpx2.ReadTimeout("stalled")),
        journal=journal,
    )
    try:
        with pytest.raises(ProviderFault):
            [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()
    assert journal.events("synthesis_pcm_emitted") == []
    assert len(journal.events("synthesis_failed")) == 1


async def test_failure_after_first_pcm_keeps_the_first_pcm_proof(openai_client, journal):
    from fakeyou.provider_faults import ProviderFault

    synthesizer = OpenAISpeechSynthesizer(
        api_key="k",
        client=openai_client(chunks=[b"\x00\x01"], stream_error=httpx2.ReadTimeout("stalled")),
        journal=journal,
    )
    try:
        with pytest.raises(ProviderFault):
            [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()
    [first] = journal.events("synthesis_pcm_emitted")
    [failed] = journal.events("synthesis_failed")
    assert first["bytes_out"] == 2
    assert failed["request_id"] == first["request_id"]
    assert journal.events("synthesis_finished") == []


async def test_cancelled_stream_after_first_pcm_keeps_the_first_pcm_proof(openai_client, journal):
    synthesizer = OpenAISpeechSynthesizer(
        client=openai_client(chunks=[b"\x00\x01", b"\x02\x03"]), journal=journal
    )
    stream = synthesizer.stream("Hello.")
    try:
        assert await anext(stream) == b"\x00\x01"
    finally:
        await stream.aclose()
        await synthesizer.aclose()
    [first] = journal.events("synthesis_pcm_emitted")
    assert first["bytes_out"] == 2
    assert journal.events("synthesis_finished") == []


async def test_minimax_first_pcm_is_reported_once(journal):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=_sse(
                {"data": {"audio": "0001", "status": 1}, "base_resp": {"status_code": 0}},
                {"data": {"audio": "0203", "status": 1}, "base_resp": {"status_code": 0}},
            ),
        )

    synthesizer = MiniMaxSpeechSynthesizer(
        api_key="k",
        client=_minimax_client(handler),
        journal=journal,
    )
    chunks = [chunk async for chunk in synthesizer.stream("Hello.")]
    assert chunks == [b"\x00\x01", b"\x02\x03"]
    [first] = journal.events("synthesis_pcm_emitted")
    [finished] = journal.events("synthesis_finished")
    assert first["source"] == "tts"
    assert first["bytes_out"] == 2
    assert finished["bytes_out"] == 4
    assert finished["request_id"] == first["request_id"]


def test_create_speech_synthesizer_picks_provider_defaults() -> None:
    minimax = create_speech_synthesizer(MiniMaxTTS(api_key="k"))
    openai = create_speech_synthesizer(OpenAITTS(api_key="k", voice="nova"))

    assert isinstance(minimax, MiniMaxSpeechSynthesizer)
    assert (minimax._model, minimax._voice) == (MiniMaxTTS().model, MiniMaxTTS().voice)
    assert isinstance(openai, OpenAISpeechSynthesizer)
    assert (openai._model, openai._voice) == (DEFAULTS.tts.model, "nova")


async def test_synthesizer_tags_sentence_synthesis_with_its_index(journal, openai_client) -> None:
    client = openai_client(chunks=[b"\x00" * 64, b"\x01" * 64])
    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=client, journal=journal)

    try:
        first = [chunk async for chunk in synthesizer.stream("First sentence.", sentence_index=0)]
        second = [chunk async for chunk in synthesizer.stream("Second sentence.", sentence_index=1)]
    finally:
        await synthesizer.aclose()

    assert first == [b"\x00" * 64, b"\x01" * 64]
    assert second == [b"\x00" * 64, b"\x01" * 64]
    assert [row["sentence_index"] for row in journal.events("synthesis_started")] == [0, 1]
    assert [row["sentence_index"] for row in journal.events("synthesis_pcm_emitted")] == [0, 1]
    assert [row["sentence_index"] for row in journal.events("synthesis_finished")] == [0, 1]
    assert [row["chars"] for row in journal.events("synthesis_started")] == [
        len("First sentence."),
        len("Second sentence."),
    ]


async def test_synthesizer_without_sentence_index_omits_the_tag(journal, openai_client) -> None:
    client = openai_client(chunks=[b"\x00" * 64])
    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=client, journal=journal)

    try:
        chunks = [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()

    assert chunks == [b"\x00" * 64]
    for row in journal.events():
        assert "sentence_index" not in row


async def test_minimax_synthesizer_tags_sentence_synthesis_with_its_index(journal) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=_sse(
                {"data": {"audio": "0001", "status": 1}, "base_resp": {"status_code": 0}},
            ),
        )

    synthesizer = MiniMaxSpeechSynthesizer(
        api_key="k",
        client=_minimax_client(handler),
        journal=journal,
    )

    chunks = [chunk async for chunk in synthesizer.stream("First sentence.", sentence_index=0)]

    assert chunks == [b"\x00\x01"]
    [started] = journal.events("synthesis_started")
    [first] = journal.events("synthesis_pcm_emitted")
    [finished] = journal.events("synthesis_finished")
    assert (started["sentence_index"], first["sentence_index"], finished["sentence_index"]) == (
        0,
        0,
        0,
    )
