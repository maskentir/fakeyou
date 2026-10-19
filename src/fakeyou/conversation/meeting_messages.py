from collections.abc import Sequence
from typing import Any

from langchain_core.messages import HumanMessage

from fakeyou.foundation.transcript import TranscriptEvent


def request_messages(events: Sequence[TranscriptEvent]) -> list[HumanMessage]:
    messages: list[HumanMessage] = []
    for event in events:
        meeting: dict[str, Any] = {
            "timestamp": event.timestamp,
            "source": event.source,
            "participant_id": event.participant_id,
            "speaker_evidence_id": event.speaker_evidence_id,
            "stream_id": event.stream_id,
            "speaker_status": "observed" if event.participant_id != "unknown" else "unknown",
        }
        if event.visual_refs:
            meeting["visual_refs"] = list(event.visual_refs)
        messages.append(
            HumanMessage(
                content=event.text,
                id=event.id,
                additional_kwargs={"meeting_event": meeting},
            )
        )
    return messages
