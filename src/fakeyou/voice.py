from collections.abc import Sequence
from typing import Literal

from pipecat.services.meta.stt import MetaSTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transcriptions.language import Language

MuseMode = Literal["ENDPOINTING", "PUSH_TO_TALK", "DIARIZATION"]


def create_muse_stt(
    *,
    api_key: str,
    language: Language | str | None = Language.ID,
    language_bias: Sequence[Language] | None = None,
    keywords: Sequence[str] | None = None,
    mode: MuseMode = "ENDPOINTING",
    sample_rate: int | None = None,
) -> MetaSTTService:
    if mode not in {"ENDPOINTING", "PUSH_TO_TALK", "DIARIZATION"}:
        raise ValueError(f"unsupported Muse mode: {mode}")
    settings = MetaSTTService.Settings(
        language=language,
        language_bias=list(language_bias) if language_bias is not None else None,
        keywords=list(keywords) if keywords is not None else None,
        mode=mode,
        emit_audio_progress=False,
    )
    return MetaSTTService(api_key=api_key, sample_rate=sample_rate, settings=settings)


def create_openai_tts(
    *,
    api_key: str | None = None,
    voice: str = "alloy",
    model: str = "gpt-4o-mini-tts",
    language: Language | str | None = None,
    instructions: str | None = None,
    speed: float | None = None,
    sample_rate: int | None = None,
) -> OpenAITTSService:
    settings = OpenAITTSService.Settings(
        model=model,
        voice=voice,
        language=language,
        instructions=instructions,
        speed=speed,
    )
    return OpenAITTSService(api_key=api_key, sample_rate=sample_rate, settings=settings)
