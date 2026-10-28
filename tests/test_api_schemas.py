import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from fakeyou.api.schemas import SessionCreate, build_snapshot
from fakeyou.catalog.errors import FakeYouError

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
AT = "2026-10-03T12:00:00+00:00"


def _example_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    persona = json.loads((EXAMPLES / "persona.example.json").read_text(encoding="utf-8"))
    brief = json.loads((EXAMPLES / "meeting-brief.example.json").read_text(encoding="utf-8"))
    brief.pop("meeting_id", None)
    return persona, brief


@pytest.fixture
def session_payload() -> dict[str, Any]:
    persona, brief = _example_documents()
    return {
        "link": "https://meet.google.com/abc-defg-hij",
        "meeting_id": "brief-demo-1003",
        "owner_id": "demo_owner",
        "twin_id": "demo_twin",
        "audio": True,
        "conversation": True,
        "persona": persona,
        "brief": brief,
    }


def test_session_create_accepts_inline_documents(session_payload: dict[str, Any]) -> None:
    create = SessionCreate.model_validate(session_payload)
    assert create.persona is not None
    assert create.persona.owner_id == "demo_owner"
    assert create.brief is not None
    assert not hasattr(create.brief, "meeting_id")
    snapshot = build_snapshot(create, meeting_id="brief-demo-1003", at=AT)
    assert snapshot.brief is not None
    assert snapshot.brief.meeting_id == "brief-demo-1003"
    assert build_snapshot(create, meeting_id="brief-demo-1003", at=AT) == snapshot


def test_session_create_rejects_the_same_combinations_as_today(
    session_payload: dict[str, Any],
) -> None:
    broken = [
        {**session_payload, "link": "https://example.com/abc-defg-hij"},
        {**session_payload, "initial_speech": "hello", "audio": False},
        {**session_payload, "memory": True, "conversation": False},
        {**session_payload, "profile_name": "../outside"},
        {**session_payload, "persona_path": "/tmp/persona.json"},
    ]
    for payload in broken:
        with pytest.raises((ValidationError, FakeYouError)):
            SessionCreate.model_validate(payload)
