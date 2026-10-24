from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from langchain_core.callbacks import BaseCallbackManager
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable

from fakeyou.foundation.transcript import (
    TranscriptEvent,
)

AgentT = Runnable[dict[str, list[BaseMessage]], dict[str, Any]]


def event(
    id: str,
    text: str = "hello",
    *,
    finalized: bool = True,
    participant_id: str = "unknown",
    speaker_evidence_id: str | None = None,
) -> TranscriptEvent:
    return TranscriptEvent(
        id,
        datetime.now(UTC).isoformat(),
        "meeting_audio",
        text,
        finalized,
        participant_id=participant_id,
        speaker_evidence_id=speaker_evidence_id,
    )


def voiced_event(
    id: str, text: str, *, participant_id: str, voice_clusters: tuple[str, ...]
) -> TranscriptEvent:
    return replace(event(id, text, participant_id=participant_id), voice_clusters=voice_clusters)


def callback_handlers(config: Mapping[str, Any] | None) -> list[Any]:
    callbacks = (config or {}).get("callbacks") or ()
    if isinstance(callbacks, BaseCallbackManager):
        return list(callbacks.handlers)
    return list(callbacks)
