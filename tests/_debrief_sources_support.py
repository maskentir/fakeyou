import json
from datetime import UTC, datetime
from typing import Any

import pytest

from fakeyou.config.identifiers import SessionId
from fakeyou.debrief.debrief_sources import SourceEntry
from fakeyou.persistence.brief import BriefItem, MeetingBrief

AT = datetime(2026, 10, 3, tzinfo=UTC)


def transcript(**changes: Any) -> dict[str, Any]:
    return {
        "kind": "transcript",
        "at": "2026-10-03T12:00:00Z",
        "id": "entry-1",
        "spoken_at": "12:00:00",
        "source": "caption",
        "text": "Please ask the owner about the new deadline.",
        "participant_id": "unknown",
        "speaker_evidence_id": None,
    } | changes


def screen(**changes: Any) -> dict[str, Any]:
    return {
        "kind": "screen",
        "at": "2026-10-03T12:00:05Z",
        "ref": "screen-1",
        "shown_at": "2026-10-03T12:00:03Z",
        "sha256": "ab" * 32,
        "presenter_id": "devices/7",
        "presenter": "Rina",
        "content": "slide",
        "title": "Q3 revenue",
        "text": "Quarterly revenue grew twelve percent.",
        "key_points": ["Revenue is up"],
        "figures": ["12%"],
        "visuals": ["bar chart"],
        "people": [],
    } | changes


def chat(**changes: Any) -> dict[str, Any]:
    return {
        "kind": "chat",
        "at": "2026-10-03T12:00:09Z",
        "ref": "chat-1",
        "message_id": "message-1",
        "sender_id": "devices/8",
        "sender": "Budi",
        "text": "I will send the deck tonight.",
        "sent_at": "2026-10-03T12:00:08Z",
    } | changes


def encoded(*entries: dict[str, Any]) -> bytes:
    return ("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n").encode()


def source(
    index: int = 1, text: str = "Please ask the owner about the new deadline."
) -> SourceEntry:
    return SourceEntry(
        id=f"transcript:entry-{index}",
        text=text,
        kind="participant",
        participant_id="unknown",
        at=AT,
    )


@pytest.fixture
def brief() -> MeetingBrief:
    return MeetingBrief(
        schema_version="meeting_brief/v1",
        owner_id=SessionId("source-owner"),
        twin_id=SessionId("source-twin"),
        meeting_id=SessionId("source-meeting"),
        objectives=(BriefItem(id="shared", text="Find blockers."),),
        questions=(BriefItem(id="shared", text="What is needed?"),),
        approved_facts=(BriefItem(id="fact", text="A venue exists."),),
    )
