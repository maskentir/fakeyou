import json
from collections.abc import Callable

import httpx2
import pytest
from openai import AsyncOpenAI

from fakeyou.config.settings import DEFAULTS, OpenAITTS
from fakeyou.speech.openai import OpenAISpeechSynthesizer, tts_voice_instructions
from fakeyou.speech.streaming import (
    TTS_HTTP_TIMEOUT,
    TTS_REQUEST_TIMEOUT_SECONDS,
)


async def test_tts_http_client_bounds_slow_streams() -> None:
    synthesizer = OpenAISpeechSynthesizer(api_key="test-key")
    client = synthesizer.client
    try:
        assert client.timeout == httpx2.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        assert client.timeout == TTS_HTTP_TIMEOUT
    finally:
        await synthesizer.aclose()
    assert client.is_closed()


async def test_synthesizer_rejects_blank_text_before_request(
    openai_client: Callable[..., AsyncOpenAI],
) -> None:
    requests: list[httpx2.Request] = []
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
        OpenAISpeechSynthesizer(api_key="test-openai-key", settings=OpenAITTS(voice="robot"))


async def test_synthesizer_closes_owned_http_client() -> None:
    synthesizer = OpenAISpeechSynthesizer(api_key="test-openai-key")
    owned = synthesizer.client

    assert owned is not None
    assert not owned.is_closed()
    await synthesizer.aclose()
    await synthesizer.aclose()
    assert owned.is_closed()


def test_synthesizer_disables_sdk_retries() -> None:
    synthesizer = OpenAISpeechSynthesizer(api_key="test-openai-key")
    assert synthesizer.client.max_retries == 0


async def test_synthesizer_timeout_raises_typed_provider_fault(
    openai_client: Callable[..., AsyncOpenAI],
) -> None:
    from fakeyou.observability.provider_faults import ProviderFault

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


async def test_synthesizer_payment_status_raises_typed_provider_fault(
    openai_client: Callable[..., AsyncOpenAI],
) -> None:
    from fakeyou.observability.provider_faults import ProviderFault

    synthesizer = OpenAISpeechSynthesizer(api_key="k", client=openai_client(status=402))

    try:
        with pytest.raises(ProviderFault) as caught:
            [chunk async for chunk in synthesizer.stream("Hello.")]
    finally:
        await synthesizer.aclose()

    assert caught.value.source == "tts"
    assert caught.value.kind == "payment_required"
    assert caught.value.status == 402


async def test_synthesizer_streams_a_pcm_request(openai_client: Callable[..., AsyncOpenAI]) -> None:
    requests: list[httpx2.Request] = []
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
