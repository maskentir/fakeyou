from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.runnables import Runnable

from fakeyou.foundation.transcript import (
    TranscriptEvent,
)

AgentT = Runnable[dict[str, list[HumanMessage]], dict[str, Any]]


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
