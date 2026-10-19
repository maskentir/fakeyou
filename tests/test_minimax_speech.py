import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from fakeyou.config.settings import DEFAULTS, MiniMaxTTS
from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE
from fakeyou.speech.languages import SPEECH_LANGUAGES
from fakeyou.speech.minimax import MiniMaxSpeechSynthesizer
from tests._persistence_fakes import RecordingJournal


def _minimax_client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _sse(*events: dict[str, Any]) -> bytes:
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
        "language_boost": SPEECH_LANGUAGES[DEFAULTS.speech.language].english,
        "voice_setting": {"voice_id": MiniMaxTTS().voice},
        "audio_setting": {"sample_rate": PLAYBACK_SAMPLE_RATE, "format": "pcm", "channel": 1},
        "stream_options": {"exclude_aggregated_audio": True},
    }


async def test_minimax_stream_yields_chunks_and_skips_the_aggregated_final_event(
    journal: RecordingJournal,
) -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
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
async def test_minimax_error_status_raises_typed_provider_fault(
    status_code: int, kind: str
) -> None:
    from fakeyou.observability.provider_faults import ProviderFault

    def handler(_request: httpx2.Request) -> httpx2.Response:
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
    from fakeyou.observability.provider_faults import ProviderFault

    synthesizer = MiniMaxSpeechSynthesizer(
        api_key="k", client=_minimax_client(lambda _request: httpx2.Response(503))
    )

    with pytest.raises(ProviderFault) as caught:
        [chunk async for chunk in synthesizer.stream("Hello.")]

    assert (caught.value.kind, caught.value.retryable) == ("unavailable", True)


async def test_minimax_synthesizer_closes_only_its_own_client() -> None:
    owned = MiniMaxSpeechSynthesizer(api_key="k")
    shared = _minimax_client(lambda _request: httpx2.Response(200))
    borrowed = MiniMaxSpeechSynthesizer(api_key="k", client=shared)

    await owned.aclose()
    await borrowed.aclose()

    assert owned.client.is_closed
    assert not shared.is_closed
    await shared.aclose()


async def test_minimax_first_pcm_is_reported_once(journal: RecordingJournal) -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
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


async def test_minimax_synthesizer_tags_sentence_synthesis_with_its_index(
    journal: RecordingJournal,
) -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
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
