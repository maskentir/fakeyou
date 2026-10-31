from collections.abc import AsyncIterator, Mapping
from dataclasses import replace
from datetime import datetime
from uuid import UUID

import httpx2
import pytest

from fakeyou.api.errors import CONFLICT_CODES, http_status_for
from fakeyou.api.memory import MemoryService
from fakeyou.api.schemas import SessionCreate, build_snapshot
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.db.session_status import PhaseUpdate
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.owner_note_service import NoteCommand
from fakeyou.memory.owner_notes import NOTE_CONFLICT_CODES, NoteConflictError, OwnerNoteResult
from fakeyou.memory.persona import PersonaProfile
from fakeyou.persistence.ingestion_status import IngestionState, IngestionStatus, write_status
from fakeyou.persistence.layout import MeetingDir
from fakeyou.transport.schemas import INGESTION_STREAM, REEMBED_STREAM
from tests.test_api_sessions import (
    AT,
    SESSION_PAYLOAD,
    STATUS_AT,
    ApiHarness,
    RecordingTransport,
)

NOTE_ID = UUID("5b7a3c1e-2f4d-4a6b-8c9d-0e1f2a3b4c5d")
TARGET = UUID("9c8b7a6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d")
SOURCE_AT = "2026-10-01T09:00:00+00:00"
RECORDED_AT = datetime.fromisoformat("2026-10-02T10:00:00+00:00")
NOTE_TEXT = "Rani owns the checklist."


class JobRecordingTransport(RecordingTransport):
    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[tuple[str, dict[str, str]]] = []
        self.entries: dict[tuple[str, str], dict[str, str]] = {}
        self.staged: dict[str, dict[str, str]] = {}

    async def store_overrides(
        self, scope: str, overrides: Mapping[str, str], *, ttl_seconds: float
    ) -> None:
        del ttl_seconds
        self.staged[scope] = dict(overrides)

    async def publish_job(self, stream: str, fields: Mapping[str, str]) -> str:
        self.jobs.append((stream, dict(fields)))
        entry_id = f"{len(self.jobs)}-0"
        self.entries[(stream, entry_id)] = dict(fields)
        return entry_id

    async def read_entry(self, stream: str, entry_id: str) -> dict[str, str] | None:
        return self.entries.get((stream, entry_id))

    def seed_entry(self, stream: str, entry_id: str, fields: dict[str, str]) -> None:
        self.entries[(stream, entry_id)] = dict(fields)

    def trim_entry(self, stream: str, entry_id: str) -> None:
        self.entries.pop((stream, entry_id), None)


class NoteServiceDouble:
    def __init__(self) -> None:
        self.applied: list[tuple[OperatorAccess, NoteCommand]] = []
        self.forgotten: list[tuple[OperatorAccess, str]] = []
        self.failure: FakeYouError | None = None

    def fail_next_with(self, error: FakeYouError) -> None:
        self.failure = error

    def _raise_pending(self) -> None:
        failure, self.failure = self.failure, None
        if failure is not None:
            raise failure

    async def apply_note(self, access: OperatorAccess, command: NoteCommand) -> OwnerNoteResult:
        self.applied.append((access, command))
        self._raise_pending()
        source_at = command.source_at if command.source_at is not None else RECORDED_AT
        supersedes = str(command.supersedes) if command.supersedes is not None else None
        return OwnerNoteResult(
            episode_uuid=str(command.note_id),
            facts=(command.text,),
            source_at=source_at,
            recorded_at=RECORDED_AT,
            supersedes=supersedes,
        )

    async def forget_note(self, access: OperatorAccess, episode_uuid: str) -> None:
        self.forgotten.append((access, episode_uuid))
        self._raise_pending()


class MemoryHarness(ApiHarness):
    transport: JobRecordingTransport

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.transport = JobRecordingTransport()
        self.notes = NoteServiceDouble()
        service = MemoryService(
            apply_note=self.notes.apply_note, forget_note=self.notes.forget_note
        )
        self.app.state.deps = replace(self.app.state.deps, memory_service=lambda: service)

    async def delete(self, path: str, *, principal: str | None = "owner") -> httpx2.Response:
        return await self.client.delete(path, headers=self._headers(principal, None))

    def seed_twin(self, meeting_id: str, *, owner_id: str = "demo_owner") -> PersonaProfile:
        twin_id = "demo_twin" if owner_id == "demo_owner" else "other_twin"
        persona = {**SESSION_PAYLOAD["persona"], "owner_id": owner_id, "twin_id": twin_id}
        create = SessionCreate.model_validate(
            {
                "link": "https://meet.google.com/abc-defg-hij",
                "meeting_id": meeting_id,
                "owner_id": owner_id,
                "twin_id": twin_id,
                "audio": True,
                "conversation": True,
                "persona": persona,
            }
        )
        snapshot = build_snapshot(create, meeting_id=meeting_id, at=AT)
        self.store.save_new(snapshot)
        assert snapshot.persona is not None
        return snapshot.persona

    def seed_session(self, meeting_id: str, *, owner_id: str = "demo_owner") -> None:
        twin_id = "demo_twin" if owner_id == "demo_owner" else "other_twin"
        create = SessionCreate.model_validate(
            {
                "link": "https://meet.google.com/abc-defg-hij",
                "meeting_id": meeting_id,
                "owner_id": owner_id,
                "twin_id": twin_id,
            }
        )
        self.store.save_new(build_snapshot(create, meeting_id=meeting_id, at=AT))

    def persist_ingestion(self, meeting_id: str, state: IngestionState) -> None:
        path = MeetingDir(self.settings.paths.record_dir, SessionId(meeting_id)).ingestion
        write_status(path, IngestionStatus(meeting_id=meeting_id, group_id="group", status=state))


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[MemoryHarness]:
    harness = MemoryHarness(settings)
    try:
        yield harness
    finally:
        await harness.aclose()


async def test_ingestion_status_is_pending_before_any_persisted_status(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    response = await api.get("/v1/sessions/m-1/ingestion")
    assert response.status_code == 200
    assert response.json() == {"status": "pending", "completed": False, "succeeded": False}


async def test_submit_ingestion_publishes_exactly_the_contract_job(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    response = await api.post("/v1/sessions/m-1/ingestions", json={"kind": "correct"})
    assert response.status_code == 202
    assert response.json() == {
        "job_id": "1-0",
        "kind": "correct",
        "meeting_id": "m-1",
        "status": "pending",
        "completed": False,
        "succeeded": False,
    }
    assert api.transport.jobs == [
        (
            "fakeyou:jobs:ingestion",
            {"v": "1", "type": "ingestion_job", "at": AT, "meeting_id": "m-1", "kind": "correct"},
        )
    ]


async def test_submit_ingestion_rejects_unknown_kinds_without_publishing(
    api: MemoryHarness,
) -> None:
    api.seed_session("m-1")
    response = await api.post("/v1/sessions/m-1/ingestions", json={"kind": "reembed"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "input.invalid"
    assert api.transport.jobs == []


@pytest.mark.parametrize(
    ("state", "completed", "succeeded"),
    [
        ("completed", True, True),
        ("failed", True, False),
        ("interrupted", True, False),
        ("pending", False, False),
    ],
)
async def test_ingestion_view_reads_the_persisted_status(
    api: MemoryHarness, *, state: IngestionState, completed: bool, succeeded: bool
) -> None:
    api.seed_session("m-1")
    api.persist_ingestion("m-1", state)
    body = (await api.get("/v1/sessions/m-1/ingestion")).json()
    assert body == {"status": state, "completed": completed, "succeeded": succeeded}


async def test_job_route_answers_from_the_owner_meeting_status(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    await api.post("/v1/sessions/m-1/ingestions", json={"kind": "ingest"})
    api.persist_ingestion("m-1", "completed")
    response = await api.get("/v1/jobs/1-0")
    assert response.status_code == 200
    assert response.json() == {
        "job_id": "1-0",
        "kind": "ingest",
        "meeting_id": "m-1",
        "status": "completed",
        "completed": True,
        "succeeded": True,
    }


async def test_persisted_status_survives_a_trimmed_job_entry(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    await api.post("/v1/sessions/m-1/ingestions", json={"kind": "ingest"})
    api.persist_ingestion("m-1", "failed")
    api.transport.trim_entry(INGESTION_STREAM, "1-0")
    assert (await api.get("/v1/jobs/1-0")).status_code == 404
    body = (await api.get("/v1/sessions/m-1/ingestion")).json()
    assert body == {"status": "failed", "completed": True, "succeeded": False}


@pytest.mark.parametrize("job_id", ["9-0", "not-an-id", "1"])
async def test_unknown_or_malformed_job_ids_are_not_found(api: MemoryHarness, job_id: str) -> None:
    response = await api.get(f"/v1/jobs/{job_id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api.not_found"


async def test_job_of_another_owner_is_not_found(api: MemoryHarness) -> None:
    api.seed_session("m-2", owner_id="other_owner")
    api.transport.seed_entry(
        INGESTION_STREAM,
        "7-0",
        {"v": "1", "type": "ingestion_job", "at": AT, "meeting_id": "m-2", "kind": "ingest"},
    )
    response = await api.get("/v1/jobs/7-0")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api.not_found"


async def test_ingestion_routes_hide_other_owner_sessions(api: MemoryHarness) -> None:
    api.seed_session("m-2", owner_id="other_owner")
    assert (await api.get("/v1/sessions/m-2/ingestion")).status_code == 404
    submitted = await api.post("/v1/sessions/m-2/ingestions", json={"kind": "ingest"})
    assert submitted.status_code == 404
    assert api.transport.jobs == []


def _note_body(**overrides: object) -> dict[str, object]:
    return {"text": NOTE_TEXT, "source_at": SOURCE_AT, "note_id": str(NOTE_ID), **overrides}


async def test_add_note_returns_the_existing_result_shape(api: MemoryHarness) -> None:
    persona = api.seed_twin("m-1")

    response = await api.post("/v1/twins/demo_twin/notes", json=_note_body())

    assert response.status_code == 201
    assert response.json() == {
        "episode_uuid": str(NOTE_ID),
        "facts": [NOTE_TEXT],
        "source_at": SOURCE_AT,
        "recorded_at": RECORDED_AT.isoformat(),
        "supersedes": None,
    }
    [(access, command)] = api.notes.applied
    assert command == NoteCommand(
        text=NOTE_TEXT,
        source_at=datetime.fromisoformat(SOURCE_AT),
        note_id=NOTE_ID,
        supersedes=None,
    )
    assert access.identity.owner_id == "demo_owner"
    assert access.identity.twin_id == "demo_twin"
    assert access.identity.persona_path is not None
    assert PersonaProfile.model_validate_json(access.identity.persona_path.read_bytes()) == persona
    assert str(api.settings.paths.record_dir) not in response.text


async def test_correction_takes_supersedes_from_the_frozen_path(api: MemoryHarness) -> None:
    api.seed_twin("m-1")

    response = await api.post(f"/v1/twins/demo_twin/notes/{TARGET}/corrections", json=_note_body())

    assert response.status_code == 201
    assert response.json()["supersedes"] == str(TARGET)
    [(_access, command)] = api.notes.applied
    assert command.supersedes == TARGET
    assert command.note_id == NOTE_ID


async def test_note_conflict_maps_to_409_with_domain_code(api: MemoryHarness) -> None:
    api.seed_twin("m-1")
    api.notes.fail_next_with(NoteConflictError(ErrorCode.NOTE_CORRECTION_CONCURRENT))

    response = await api.post(f"/v1/twins/demo_twin/notes/{TARGET}/corrections", json=_note_body())

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "note.correction_concurrent"
    assert error["details"] == {"episode_uuid": str(TARGET), "retryable": True}


@pytest.mark.parametrize("code", sorted(NOTE_CONFLICT_CODES, key=str))
async def test_every_note_conflict_code_is_a_409_on_add(
    api: MemoryHarness, code: ErrorCode
) -> None:
    api.seed_twin("m-1")
    api.notes.fail_next_with(NoteConflictError(code))

    response = await api.post("/v1/twins/demo_twin/notes", json=_note_body())

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == code.value
    assert error["details"] == {
        "episode_uuid": str(NOTE_ID),
        "retryable": code is ErrorCode.NOTE_CORRECTION_CONCURRENT,
    }


def test_note_conflict_codes_extend_the_api_conflict_set() -> None:
    assert {code.value for code in NOTE_CONFLICT_CODES} <= CONFLICT_CODES
    for code in NOTE_CONFLICT_CODES:
        assert http_status_for(NoteConflictError(code)) == 409
    assert http_status_for(FakeYouError(ErrorCode.NOTE_HAS_PENDING_CORRECTION)) == 400


async def test_correction_target_must_be_a_uuid(api: MemoryHarness) -> None:
    api.seed_twin("m-1")

    response = await api.post("/v1/twins/demo_twin/notes/ep-1/corrections", json=_note_body())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "memory.episode_not_uuid"
    assert api.notes.applied == []


async def test_note_body_requires_a_client_note_id(api: MemoryHarness) -> None:
    api.seed_twin("m-1")

    response = await api.post(
        "/v1/twins/demo_twin/notes", json={"text": NOTE_TEXT, "source_at": SOURCE_AT}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "input.invalid"
    assert api.notes.applied == []


async def test_forget_note_removes_the_episode_through_the_twin_access(api: MemoryHarness) -> None:
    api.seed_twin("m-1")

    response = await api.delete(f"/v1/twins/demo_twin/notes/{TARGET}")

    assert response.status_code == 204
    assert response.content == b""
    [(access, episode_uuid)] = api.notes.forgotten
    assert episode_uuid == str(TARGET)
    assert access.identity.twin_id == "demo_twin"


async def test_forget_note_errors_outside_the_conflict_set_stay_400(api: MemoryHarness) -> None:
    api.seed_twin("m-1")
    api.notes.fail_next_with(FakeYouError(ErrorCode.NOTE_HAS_PENDING_CORRECTION))

    response = await api.delete(f"/v1/twins/demo_twin/notes/{TARGET}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "note.has_pending_correction"


async def test_twin_routes_hide_twins_without_an_owned_persona(api: MemoryHarness) -> None:
    api.seed_twin("m-2", owner_id="other_owner")
    api.seed_session("m-3")

    responses = [
        await api.post("/v1/twins/other_twin/notes", json=_note_body()),
        await api.post(f"/v1/twins/other_twin/notes/{TARGET}/corrections", json=_note_body()),
        await api.delete(f"/v1/twins/other_twin/notes/{TARGET}"),
        await api.post("/v1/twins/demo_twin/notes", json=_note_body()),
        await api.post("/v1/twins/unknown_twin/notes", json=_note_body()),
    ]

    assert [response.status_code for response in responses] == [404] * 5
    assert {response.json()["error"]["code"] for response in responses} == {"api.not_found"}
    assert api.notes.applied == []
    assert api.notes.forgotten == []


async def test_twin_access_uses_the_latest_persona_of_the_twin(api: MemoryHarness) -> None:
    api.seed_twin("m-1")
    api.seed_session("m-2")

    response = await api.post("/v1/twins/demo_twin/notes", json=_note_body())

    assert response.status_code == 201
    [(access, _command)] = api.notes.applied
    assert access.identity.persona_path is not None
    assert access.identity.persona_path.parent.name == "m-1"


async def test_forget_meeting_returns_the_c9_summary_with_its_stages(api: MemoryHarness) -> None:
    api.seed_twin("m-1")
    response = await api.post("/v1/sessions/m-1/forget", json={"keep_record": False})

    assert response.status_code == 200
    body = response.json()
    assert body["removed_episodes"] == 0
    assert body["complete"] is False
    assert [stage["stage"] for stage in body["stages"]] == [
        "graph",
        "record",
        "journal",
        "browser_profile",
        "status",
    ]
    assert body["stages"][0]["status"] == "failed"
    assert [stage["status"] for stage in body["stages"][1:]] == [
        "removed",
        "absent",
        "absent",
        "absent",
    ]


@pytest.mark.parametrize("keep_record", [False, True])
async def test_forget_refuses_a_meeting_that_is_still_running(
    api: MemoryHarness, *, keep_record: bool
) -> None:
    api.seed_twin("m-1")
    await api.status.create("m-1", "demo_owner", STATUS_AT)
    await api.status.record_phase("m-1", PhaseUpdate("active", STATUS_AT))

    response = await api.post("/v1/sessions/m-1/forget", json={"keep_record": keep_record})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "memory.forget_session_live"
    assert await api.status.get("m-1") is not None
    assert api.store.load("m-1") is not None


async def test_forget_meeting_outside_the_principal_is_not_found(api: MemoryHarness) -> None:
    api.seed_session("m-2", owner_id="other_owner")

    response = await api.post("/v1/sessions/m-2/forget", json={})

    assert response.status_code == 404


async def test_forget_meeting_rejects_an_unreadable_keep_record(api: MemoryHarness) -> None:
    api.seed_twin("m-1")

    response = await api.post("/v1/sessions/m-1/forget", json={"keep_record": "maybe"})

    assert response.status_code == 400


async def test_reembedding_queues_a_job_on_the_amended_stream(api: MemoryHarness) -> None:
    api.seed_twin("m-1")

    response = await api.post("/v1/twins/demo_twin/reembeddings", json={})

    assert response.status_code == 202
    body = response.json()
    assert body["kind"] == "reembed"
    stream, fields = api.transport.jobs[-1]
    assert stream == REEMBED_STREAM
    assert fields["type"] == "reembed_job"
    assert fields["twin_id"] == "demo_twin"
    assert fields["owner_id"] == "demo_owner"
    assert (REEMBED_STREAM, body["job_id"]) in api.transport.entries
    assert body["status"] == "queued"


async def test_reembedding_for_another_twin_is_not_found(api: MemoryHarness) -> None:
    api.seed_twin("m-1")

    response = await api.post("/v1/twins/other_twin/reembeddings", json={})

    assert response.status_code == 404


async def test_submit_ingestion_stages_the_request_keys_for_its_meeting(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    response = await api.post(
        "/v1/sessions/m-1/ingestions",
        json={"kind": "ingest"},
        headers={"X-Fakeyou-Key-Reply": "reply-key"},
    )

    assert response.status_code == 202
    assert api.transport.staged == {"m-1": {"reply": "reply-key"}}


async def test_submit_ingestion_without_keys_stages_nothing(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    response = await api.post("/v1/sessions/m-1/ingestions", json={"kind": "ingest"})

    assert response.status_code == 202
    assert api.transport.staged == {}
