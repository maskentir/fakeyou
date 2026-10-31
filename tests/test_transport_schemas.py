import pytest

from fakeyou.transport.schemas import (
    encode_ingestion_job,
    event_stream,
    validate_event,
)

AT = "2026-10-03T12:00:00+00:00"


def test_ingestion_job_fields_match_track_c_parser_contract() -> None:
    fields = encode_ingestion_job(meeting_id="m-1", kind="correct", at=AT)
    assert fields == {
        "v": "1",
        "type": "ingestion_job",
        "at": AT,
        "meeting_id": "m-1",
        "kind": "correct",
    }
    assert event_stream("m-1") == "fakeyou:meeting:m-1:events"


def test_validate_event_rejects_evidence_and_key_fields() -> None:
    base = {"v": "1", "type": "phase_changed", "at": AT, "meeting_id": "m-1", "phase": "active"}
    assert validate_event(base, meeting_id="m-1")["phase"] == "active"
    for extra in ({"evidence": "raw"}, {"api_key": "x"}, {"type": "audio_opened"}):
        with pytest.raises(ValueError, match="event"):
            validate_event({**base, **extra}, meeting_id="m-1")
