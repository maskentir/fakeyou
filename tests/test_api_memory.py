from collections.abc import AsyncIterator, Mapping

import pytest

from fakeyou.api.schemas import SessionCreate, build_snapshot
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.db.session_status import PhaseUpdate
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


class MemoryHarness(ApiHarness):
    transport: JobRecordingTransport

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.transport = JobRecordingTransport()

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
