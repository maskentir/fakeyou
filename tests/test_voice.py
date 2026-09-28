from typing import cast

import pytest
from pipecat.services.meta.stt import MetaSTTService
from pipecat.services.openai.tts import OpenAITTSService
from pipecat.transcriptions.language import Language

from fakeyou.voice import MuseMode, create_muse_stt, create_openai_tts


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


def test_create_openai_tts_builds_native_streaming_service() -> None:
    service = create_openai_tts(
        api_key="test-openai-key",
        voice="marin",
        instructions="Speak Indonesian clearly.",
    )

    assert isinstance(service, OpenAITTSService)
    assert service._settings.voice == "marin"
    assert service._settings.model == "gpt-4o-mini-tts"
    assert service._settings.instructions == "Speak Indonesian clearly."
