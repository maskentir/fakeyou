import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.debrief.debrief import debrief_report_status
from fakeyou.observability.journal import Journal, JournalFile, journal_path
from fakeyou.observability.report import build_session_report, read_journal_with_corrupt
from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.layout import MeetingDir
from fakeyou.transport.schemas import DEBRIEF_STREAM
from tests._debrief_support import AT as RECORD_AT
from tests._debrief_support import report_for
from tests.test_api_memory import MemoryHarness
from tests.test_api_sessions import AT

NO_REPORT = {"status": "missing", "review_status": "unreviewed", "cached": False, "report": None}


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[MemoryHarness]:
    harness = MemoryHarness(settings)
    try:
        yield harness
    finally:
        await harness.aclose()


def _seed_record(api: MemoryHarness, meeting_id: str) -> Path:
    record = MeetingDir(api.settings.paths.record_dir, SessionId(meeting_id)).record
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps(
            {
                "kind": "transcript",
                "at": RECORD_AT.isoformat(),
                "id": "e1",
                "spoken_at": "12:00:00",
                "source": "caption",
                "text": "Ask the owner about the deadline.",
                "participant_id": "unknown",
                "speaker_evidence_id": None,
            }
        )
        + "\n"
    )
    return record


def _seed_debrief(api: MemoryHarness, meeting_id: str) -> Path:
    record = _seed_record(api, meeting_id)
    report = report_for(
        record,
        owner_id="demo_owner",
        twin_id="demo_twin",
        meeting_id=meeting_id,
    )
    artifact = MeetingDir(api.settings.paths.record_dir, SessionId(meeting_id)).debrief_json
    write_atomically(artifact, report.model_dump_json().encode("utf-8"))
    return record


@pytest.mark.parametrize(("force", "wire"), [(True, "true"), (False, "false")])
async def test_submit_debrief_publishes_the_frozen_payload(
    api: MemoryHarness, *, force: bool, wire: str
) -> None:
    api.seed_session("m-1")
    response = await api.post("/v1/sessions/m-1/debriefs", json={"force": force})
    assert response.status_code == 202
    assert response.json() == {"meeting_id": "m-1", "status": "queued"}
    assert api.transport.jobs == [
        (
            DEBRIEF_STREAM,
            {"v": "1", "type": "debrief_job", "at": AT, "meeting_id": "m-1", "force": wire},
        )
    ]


async def test_submit_debrief_requires_the_force_flag(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    response = await api.post("/v1/sessions/m-1/debriefs", json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "input.invalid"
    assert api.transport.jobs == []


async def test_debrief_without_an_artifact_is_missing_and_null(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    response = await api.get("/v1/sessions/m-1/debrief")
    assert response.status_code == 200
    assert response.json() == NO_REPORT


async def test_debrief_serves_the_parsed_server_side_artifact(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    record = _seed_debrief(api, "m-1")
    response = await api.get("/v1/sessions/m-1/debrief")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == debrief_report_status(record)
    assert body["review_status"] == "unreviewed"
    assert body["cached"] is True
    assert body["report"]["meeting_id"] == "m-1"
    assert body["report"]["review_status"] == "unreviewed"
    assert "json_path" not in response.text
    assert "markdown_path" not in response.text
    assert str(api.settings.paths.record_dir) not in response.text


async def test_unparseable_debrief_artifact_serves_a_null_report(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    record = _seed_record(api, "m-1")
    artifact = MeetingDir(api.settings.paths.record_dir, SessionId("m-1")).debrief_json
    write_atomically(artifact, b"{not json")
    body = (await api.get("/v1/sessions/m-1/debrief")).json()
    assert body == {
        "status": debrief_report_status(record),
        "review_status": "unreviewed",
        "cached": False,
        "report": None,
    }


async def test_debrief_routes_hide_other_owner_sessions(api: MemoryHarness) -> None:
    api.seed_session("m-2", owner_id="other_owner")
    assert (await api.post("/v1/sessions/m-2/debriefs", json={"force": True})).status_code == 404
    assert (await api.get("/v1/sessions/m-2/debrief")).status_code == 404
    assert (await api.get("/v1/sessions/m-2/report")).status_code == 404
    assert api.transport.jobs == []


def _seed_journal(settings: Settings, meeting_id: str) -> None:
    journal = Journal(
        meeting_id,
        file=JournalFile(journal_path(settings.paths.journal_dir, SessionId(meeting_id))),
        echo=False,
    )
    journal.record(
        "session_finished", source="cli", terminal_reason="stopped_by_owner", exit_code=0
    )
    journal.close()


async def test_report_matches_the_journal_composition(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    _seed_journal(api.settings, "m-1")
    rows, corrupt = read_journal_with_corrupt(
        journal_path(api.settings.paths.journal_dir, SessionId("m-1"))
    )
    expected = {"meeting_id": "m-1", **build_session_report(rows, corrupt_lines=corrupt)}
    expected["debrief"] = {"status": "missing"}
    response = await api.get("/v1/sessions/m-1/report")
    assert response.status_code == 200
    assert response.json() == expected
    assert response.json()["result"]["terminal_reason"] == "stopped_by_owner"


async def test_report_without_a_journal_names_only_the_relative_file(
    api: MemoryHarness,
) -> None:
    api.seed_session("m-1")
    response = await api.get("/v1/sessions/m-1/report")
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "diagnostics.journal_missing"
    assert str(api.settings.paths.journal_dir) not in response.text
    assert "m-1.jsonl" in error["message"]
