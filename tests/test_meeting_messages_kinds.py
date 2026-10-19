from datetime import UTC, datetime

import pytest

from fakeyou.conversation.meeting_messages import request_messages
from fakeyou.foundation.transcript import EventSource, TranscriptEvent
from fakeyou.foundation.triggers import TriggerKind, trigger_kind


@pytest.mark.parametrize(
    ("source", "kind"),
    [("meeting_audio", "speech"), ("meeting_chat", "chat"), ("meeting_attention", "attention")],
)
def test_each_source_becomes_its_trigger_kind(source: EventSource, kind: TriggerKind) -> None:
    event = TranscriptEvent(
        "e1",
        datetime.now(UTC).isoformat(),
        source,
        "text",
        finalized=True,
        participant_id="device-1",
        speaker_evidence_id=None,
        display_claim="Budi",
    )

    [message] = request_messages([event])

    assert trigger_kind(message) == kind
    assert "display_claim" not in message.additional_kwargs["meeting_event"]
