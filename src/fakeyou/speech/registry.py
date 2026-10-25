from typing import assert_never

from fakeyou.catalog.config import ConfigKey
from fakeyou.config.settings import (
    CartesiaTTS,
    ElevenLabsTTS,
    MiniMaxTTS,
    OpenAITTS,
    TTSSettings,
    require_secret,
)
from fakeyou.observability.journal import Journal
from fakeyou.speech.cartesia import CartesiaSpeechSynthesizer
from fakeyou.speech.contract import SpeechSynthesizer
from fakeyou.speech.elevenlabs import ElevenLabsSpeechSynthesizer
from fakeyou.speech.fallback import SpeechWithFallback
from fakeyou.speech.minimax import MiniMaxSpeechSynthesizer
from fakeyou.speech.openai import OpenAISpeechSynthesizer


def build_speech_synthesizer(
    settings: TTSSettings,
    *,
    language: str,
    journal: Journal | None = None,
    fallback: TTSSettings | None = None,
) -> SpeechSynthesizer:
    primary = _provider_synthesizer(
        settings, ConfigKey.TTS_API_KEY, language=language, journal=journal
    )
    if fallback is None:
        return primary
    return SpeechWithFallback(
        primary,
        _provider_synthesizer(
            fallback, ConfigKey.TTS_FALLBACK_API_KEY, language=language, journal=journal
        ),
        journal=journal,
    )


def _provider_synthesizer(
    settings: TTSSettings,
    key: ConfigKey,
    *,
    language: str,
    journal: Journal | None,
) -> SpeechSynthesizer:
    api_key = require_secret(settings.api_key, key)
    match settings:
        case OpenAITTS():
            return OpenAISpeechSynthesizer(
                api_key=api_key, settings=settings, language=language, journal=journal
            )
        case MiniMaxTTS():
            return MiniMaxSpeechSynthesizer(
                api_key=api_key, settings=settings, language=language, journal=journal
            )
        case CartesiaTTS():
            return CartesiaSpeechSynthesizer(
                api_key=api_key, settings=settings, language=language, journal=journal
            )
        case ElevenLabsTTS():
            return ElevenLabsSpeechSynthesizer(
                api_key=api_key, settings=settings, language=language, journal=journal
            )
        case _:
            assert_never(settings)
