import pytest

from fakeyou.transport.schemas import (
    validate_event,
)

AT = "2026-10-03T12:00:00+00:00"


def test_validate_event_rejects_evidence_and_key_fields() -> None:
    base = {"v": "1", "type": "phase_changed", "at": AT, "meeting_id": "m-1", "phase": "active"}
    assert validate_event(base, meeting_id="m-1")["phase"] == "active"
    for extra in ({"evidence": "raw"}, {"api_key": "x"}, {"type": "audio_opened"}):
        with pytest.raises(ValueError, match="event"):
            validate_event({**base, **extra}, meeting_id="m-1")
