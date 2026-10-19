import json
from collections.abc import Callable

import httpx2
import pytest

from fakeyou.config.settings import CartesiaTTS
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.speech.cartesia import CARTESIA_API_VERSION, CartesiaSpeechSynthesizer


def _client(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


async def test_cartesia_posts_the_transcript_to_the_bytes_endpoint_for_pcm_output() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=b"\x00\x01" * 10)

    synthesizer = CartesiaSpeechSynthesizer(
        api_key="cartesia-key",
        settings=CartesiaTTS(),
        language="id",
        client=_client(handler),
    )
    audio = b"".join([chunk async for chunk in synthesizer.stream("Hello.")])
    await synthesizer.aclose()

    assert audio == b"\x00\x01" * 10
    [request] = requests
    assert str(request.url) == "https://api.cartesia.ai/tts/bytes"
    assert request.headers["X-API-Key"] == "cartesia-key"
    assert request.headers["Cartesia-Version"] == CARTESIA_API_VERSION
    assert json.loads(request.content) == {
        "model_id": "sonic-3",
        "transcript": "Hello.",
        "voice": {"mode": "id", "id": CartesiaTTS().voice},
        "language": "id",
        "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000},
    }


async def test_cartesia_uses_the_configured_model_and_voice() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=b"\x00\x01")

    settings = CartesiaTTS(model="sonic-3-custom", voice="voice-123")
    synthesizer = CartesiaSpeechSynthesizer(
        api_key="k", settings=settings, language="en", client=_client(handler)
    )
    _ = [chunk async for chunk in synthesizer.stream("Hello.")]

    body = json.loads(requests[0].content)
    assert body["model_id"] == "sonic-3-custom"
    assert body["voice"] == {"mode": "id", "id": "voice-123"}
    assert body["language"] == "en"


async def test_cartesia_error_status_raises_a_typed_fault_without_the_key() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"error": "invalid api key"})

    synthesizer = CartesiaSpeechSynthesizer(
        api_key="cartesia-secret-key", settings=CartesiaTTS(), client=_client(handler)
    )

    with pytest.raises(ProviderFault) as caught:
        _ = [chunk async for chunk in synthesizer.stream("Hello.")]

    assert caught.value.kind == "unauthorized"
    assert "cartesia-secret-key" not in str(caught.value)


async def test_cartesia_rejects_blank_text_before_any_request() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=b"\x00\x01")

    synthesizer = CartesiaSpeechSynthesizer(
        api_key="k", settings=CartesiaTTS(), client=_client(handler)
    )

    with pytest.raises(ValueError, match="blank text"):
        _ = [chunk async for chunk in synthesizer.stream("   ")]
    assert requests == []


async def test_cartesia_closes_only_the_client_it_created(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[httpx2.AsyncClient] = []

    real_client = httpx2.AsyncClient

    def factory(**_kwargs: object) -> httpx2.AsyncClient:
        client = real_client(transport=httpx2.MockTransport(lambda _: httpx2.Response(200)))
        created.append(client)
        return client

    monkeypatch.setattr(httpx2, "AsyncClient", factory)
    owned = CartesiaSpeechSynthesizer(api_key="k", settings=CartesiaTTS())
    await owned.aclose()
    assert created[0].is_closed
