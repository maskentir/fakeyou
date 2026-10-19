import json
from collections.abc import Callable

import httpx2
import pytest
from pydantic import ValidationError

from fakeyou.config.settings import ElevenLabsTTS
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.speech.elevenlabs import ElevenLabsSpeechSynthesizer


def _client(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def test_elevenlabs_requires_an_explicit_voice() -> None:
    with pytest.raises(ValidationError):
        ElevenLabsTTS.model_validate({})


async def test_elevenlabs_streams_pcm_from_the_voice_stream_endpoint() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=b"\x00\x01" * 4)

    synthesizer = ElevenLabsSpeechSynthesizer(
        api_key="eleven-key",
        settings=ElevenLabsTTS(voice="voice-abc"),
        language="id",
        client=_client(handler),
    )
    audio = b"".join([chunk async for chunk in synthesizer.stream("Hello.")])

    assert audio == b"\x00\x01" * 4
    [request] = requests
    assert str(request.url) == (
        "https://api.elevenlabs.io/v1/text-to-speech/voice-abc/stream?output_format=pcm_24000"
    )
    assert request.headers["xi-api-key"] == "eleven-key"
    assert json.loads(request.content) == {
        "text": "Hello.",
        "model_id": "eleven_multilingual_v2",
        "language_code": "id",
    }


async def test_elevenlabs_error_status_raises_a_typed_fault_without_the_key() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"detail": "invalid key"})

    synthesizer = ElevenLabsSpeechSynthesizer(
        api_key="eleven-secret-key",
        settings=ElevenLabsTTS(voice="voice-abc"),
        client=_client(handler),
    )

    with pytest.raises(ProviderFault) as caught:
        _ = [chunk async for chunk in synthesizer.stream("Hello.")]

    assert caught.value.kind == "unauthorized"
    assert "eleven-secret-key" not in str(caught.value)


async def test_elevenlabs_rejects_blank_text_before_any_request() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=b"\x00\x01")

    synthesizer = ElevenLabsSpeechSynthesizer(
        api_key="k", settings=ElevenLabsTTS(voice="voice-abc"), client=_client(handler)
    )

    with pytest.raises(ValueError, match="blank text"):
        _ = [chunk async for chunk in synthesizer.stream(" ")]
    assert requests == []
