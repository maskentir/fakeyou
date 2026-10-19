import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fakeyou.config.identifiers import SessionId
from fakeyou.debrief.debrief_schema import DebriefItem, DebriefReport, EvidenceRef, EvidenceSource
from fakeyou.debrief.debrief_sources import SourceEntry, load_record_snapshot
from fakeyou.persistence.brief import BriefItem, MeetingBrief

AT = datetime(2026, 10, 3, 12, tzinfo=UTC)


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "correction_labels.json").read_text(encoding="utf-8")
)


def source(index: int = 1, text: str = "Ask the owner about the deadline.") -> SourceEntry:
    return SourceEntry(f"transcript:e{index}", text, "participant", "unknown", AT)


def ref(index: int = 1, quote: str = "deadline") -> EvidenceRef:
    return EvidenceRef(entry_id=f"transcript:e{index}", quote=quote)


def item(index: int = 1, **changes: Any) -> DebriefItem:
    return DebriefItem.model_validate(
        {
            "kind": "owner_decision",
            "text": "The owner must decide.",
            "status": "needs_owner",
            "evidence": [ref(index)],
        }
        | changes
    )


@pytest.fixture
def brief() -> MeetingBrief:
    return MeetingBrief(
        schema_version="meeting_brief/v1",
        owner_id=SessionId("b2-owner"),
        twin_id=SessionId("b2-twin"),
        meeting_id=SessionId("b2-meeting"),
        objectives=(BriefItem(id="o1", text="Find blockers."),),
        questions=(BriefItem(id="q1", text="Which deadline?"),),
    )


@pytest.fixture
def record_path(tmp_path: Path) -> Path:
    path = tmp_path / "b2-meeting" / "record.jsonl"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "kind": "transcript",
                "at": AT.isoformat(),
                "id": "e1",
                "spoken_at": "12:00:00",
                "source": "caption",
                "text": source().text,
                "participant_id": "unknown",
                "speaker_evidence_id": None,
            }
        )
        + "\n"
    )
    return path


def report_for(record_path: Path, **changes: Any) -> DebriefReport:
    snapshot = load_record_snapshot(record_path)
    values = {
        "schema_version": "meeting_debrief/v1",
        "owner_id": "b2-owner",
        "twin_id": "b2-twin",
        "meeting_id": "b2-meeting",
        "record_sha256": snapshot.record_sha256,
        "context_sha256": None,
        "generated_at": AT,
        "provider": "fake",
        "model": "fake-model",
        "source_count": len(snapshot.sources),
        "chunk_count": 1,
        "status": "completed",
        "review_status": "unreviewed",
        "items": [item()],
        "findings": [],
        "evidence_sources": [
            EvidenceSource(
                id=s.id, text=s.text, kind=s.kind, participant_id=s.participant_id, at=s.at
            )
            for s in snapshot.sources
        ],
        "source_limitations": ["Unknown attribution."],
    }
    return DebriefReport.model_validate(values | changes)
