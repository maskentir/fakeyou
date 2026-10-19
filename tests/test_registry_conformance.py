from typing import Any

import pytest
from pydantic import SecretStr

from fakeyou.config.settings import (
    CartesiaTTS,
    ElevenLabsTTS,
    MiniMaxTTS,
    OpenAITTS,
    Settings,
    TTSSettings,
)
from fakeyou.speech.cartesia import CartesiaSpeechSynthesizer
from fakeyou.speech.contract import SpeechSynthesizer
from fakeyou.speech.elevenlabs import ElevenLabsSpeechSynthesizer
from fakeyou.speech.minimax import MiniMaxSpeechSynthesizer
from fakeyou.speech.openai import OpenAISpeechSynthesizer
from fakeyou.speech.registry import build_speech_synthesizer

NO_ENV_FILE: dict[str, Any] = {"_env_file": None}
PROVIDERS: list[tuple[TTSSettings, type[object]]] = [
    (OpenAITTS(api_key=SecretStr("k")), OpenAISpeechSynthesizer),
    (MiniMaxTTS(api_key=SecretStr("k")), MiniMaxSpeechSynthesizer),
    (CartesiaTTS(api_key=SecretStr("k")), CartesiaSpeechSynthesizer),
    (ElevenLabsTTS(api_key=SecretStr("k"), voice="voice-abc"), ElevenLabsSpeechSynthesizer),
]


@pytest.mark.parametrize(
    ("settings", "expected"), PROVIDERS, ids=lambda value: type(value).__name__
)
async def test_registry_builds_the_adapter_for_each_provider(
    settings: TTSSettings, expected: type[object]
) -> None:
    synthesizer: SpeechSynthesizer = build_speech_synthesizer(settings, language="id")

    assert isinstance(synthesizer, expected)
    await synthesizer.aclose()


def test_provider_is_selected_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKEYOU_TTS__PROVIDER", "cartesia")
    monkeypatch.setenv("FAKEYOU_TTS__API_KEY", "cartesia-key")

    settings = Settings(**NO_ENV_FILE)

    assert isinstance(settings.tts, CartesiaTTS)
    assert settings.tts.api_key is not None
    assert settings.tts.api_key.get_secret_value() == "cartesia-key"


def test_unknown_provider_is_rejected_at_settings_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKEYOU_TTS__PROVIDER", "unknown-provider")

    with pytest.raises(ValueError, match="unknown-provider|provider"):
        Settings(**NO_ENV_FILE)


def test_speech_language_must_be_supported_by_the_selected_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(CartesiaTTS, "languages", frozenset({"en"}))
    monkeypatch.setenv("FAKEYOU_TTS__PROVIDER", "cartesia")
    monkeypatch.setenv("FAKEYOU_SPEECH__LANGUAGE", "id")

    with pytest.raises(ValueError, match="not supported by the 'cartesia' TTS provider"):
        Settings(**NO_ENV_FILE)
