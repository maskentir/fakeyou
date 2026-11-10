from collections.abc import AsyncIterator, Mapping

import pytest

from fakeyou.api.schemas import SessionCreate, build_snapshot
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.db.session_status import PhaseUpdate
from fakeyou.memory.persona import PersonaProfile
from fakeyou.persistence.ingestion_status import (
    IngestionState,
    IngestionStatus,
    JobOutcome,
    SettledJob,
    write_status,
)
from fakeyou.persistence.layout import MeetingDir
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
        self.staged: dict[str, dict[str, str]] = {}

    async def store_overrides(
        self, scope: str, overrides: Mapping[str, str], *, ttl_seconds: float
    ) -> None:
        del ttl_seconds
        self.staged[scope] = dict(overrides)


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
        self.status.seed(snapshot.model_dump(mode="json"), STATUS_AT)
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
        snapshot = build_snapshot(create, meeting_id=meeting_id, at=AT)
        self.status.seed(snapshot.model_dump(mode="json"), STATUS_AT)

    def persist_ingestion(
        self, meeting_id: str, state: IngestionState, *, settled_job: SettledJob | None = None
    ) -> None:
        path = MeetingDir(self.settings.paths.record_dir, SessionId(meeting_id)).ingestion
        write_status(
            path,
            IngestionStatus(
                meeting_id=meeting_id, group_id="group", status=state, settled_job=settled_job
            ),
        )


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


async def test_submit_ingestion_defers_exactly_the_contract_job(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    response = await api.post("/v1/sessions/m-1/ingestions", json={"kind": "correct"})
    assert response.status_code == 202
    assert response.json() == {
        "job_id": "1",
        "kind": "correct",
        "meeting_id": "m-1",
        "status": "pending",
        "completed": False,
        "succeeded": False,
    }
    queued = await api.jobs.fetch(1)
    assert queued is not None
    assert queued.task == "ingest_meeting"
    assert queued.args == {"meeting_id": "m-1", "kind": "correct"}


async def test_submit_ingestion_rejects_unknown_kinds_without_publishing(
    api: MemoryHarness,
) -> None:
    api.seed_session("m-1")
    response = await api.post("/v1/sessions/m-1/ingestions", json={"kind": "reembed"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "input.invalid"
    assert await api.jobs.jobs_for("ingest_meeting") == []


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


async def test_job_route_answers_from_the_status_its_own_job_wrote(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    submitted = (await api.post("/v1/sessions/m-1/ingestions", json={"kind": "ingest"})).json()
    api.persist_ingestion(
        "m-1", "completed", settled_job=SettledJob(id=submitted["job_id"], outcome="completed")
    )
    response = await api.get(f"/v1/jobs/{submitted['job_id']}")
    assert response.status_code == 200
    assert response.json() == {
        "job_id": submitted["job_id"],
        "kind": "ingest",
        "meeting_id": "m-1",
        "status": "completed",
        "completed": True,
        "succeeded": True,
    }


@pytest.mark.parametrize("previous", ["failed", "completed"])
async def test_submitted_job_is_pending_whatever_the_previous_run_left(
    api: MemoryHarness, previous: JobOutcome
) -> None:
    api.seed_session("m-1")
    api.persist_ingestion("m-1", previous, settled_job=SettledJob(id="0-1", outcome=previous))
    submitted = (await api.post("/v1/sessions/m-1/ingestions", json={"kind": "ingest"})).json()
    polled = (await api.get(f"/v1/jobs/{submitted['job_id']}")).json()
    assert (submitted["status"], submitted["completed"], submitted["succeeded"]) == (
        "pending",
        False,
        False,
    )
    assert (polled["status"], polled["completed"], polled["succeeded"]) == (
        "pending",
        False,
        False,
    )


async def test_job_settles_once_the_status_carries_its_id(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    api.persist_ingestion("m-1", "failed")
    submitted = (await api.post("/v1/sessions/m-1/ingestions", json={"kind": "ingest"})).json()
    assert (await api.get(f"/v1/jobs/{submitted['job_id']}")).json()["status"] == "pending"
    api.persist_ingestion(
        "m-1", "completed", settled_job=SettledJob(id=submitted["job_id"], outcome="completed")
    )
    body = (await api.get(f"/v1/jobs/{submitted['job_id']}")).json()
    assert (body["status"], body["completed"], body["succeeded"]) == ("completed", True, True)


async def test_status_of_another_job_never_leaks_into_this_job(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    first_id = (await api.post("/v1/sessions/m-1/ingestions", json={"kind": "ingest"})).json()[
        "job_id"
    ]
    second_id = (await api.post("/v1/sessions/m-1/ingestions", json={"kind": "ingest"})).json()[
        "job_id"
    ]
    api.persist_ingestion("m-1", "failed", settled_job=SettledJob(id=second_id, outcome="failed"))
    first = (await api.get(f"/v1/jobs/{first_id}")).json()
    second = (await api.get(f"/v1/jobs/{second_id}")).json()
    assert (first["status"], first["completed"]) == ("pending", False)
    assert (second["status"], second["completed"], second["succeeded"]) == ("failed", True, False)


async def test_job_outcome_is_read_apart_from_the_meeting_state(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    submitted = (await api.post("/v1/sessions/m-1/ingestions", json={"kind": "correct"})).json()
    api.persist_ingestion(
        "m-1", "pending", settled_job=SettledJob(id=submitted["job_id"], outcome="completed")
    )
    job = (await api.get(f"/v1/jobs/{submitted['job_id']}")).json()
    meeting = (await api.get("/v1/sessions/m-1/ingestion")).json()
    assert (job["status"], job["completed"], job["succeeded"]) == ("completed", True, True)
    assert meeting == {"status": "pending", "completed": False, "succeeded": False}


async def test_persisted_status_is_readable_without_the_job_row(api: MemoryHarness) -> None:
    api.seed_session("m-1")
    api.persist_ingestion("m-1", "failed")
    assert (await api.get("/v1/jobs/999")).status_code == 404
    body = (await api.get("/v1/sessions/m-1/ingestion")).json()
    assert body == {"status": "failed", "completed": True, "succeeded": False}


@pytest.mark.parametrize("job_id", ["999", "not-an-id", "1-0"])
async def test_unknown_or_malformed_job_ids_are_not_found(api: MemoryHarness, job_id: str) -> None:
    response = await api.get(f"/v1/jobs/{job_id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api.not_found"


async def test_job_of_another_owner_is_not_found(api: MemoryHarness) -> None:
    api.seed_session("m-2", owner_id="other_owner")
    job_id = await api.jobs.defer_ingest(meeting_id="m-2", kind="ingest")
    response = await api.get(f"/v1/jobs/{job_id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api.not_found"


async def test_ingestion_routes_hide_other_owner_sessions(api: MemoryHarness) -> None:
    api.seed_session("m-2", owner_id="other_owner")
    assert (await api.get("/v1/sessions/m-2/ingestion")).status_code == 404
    submitted = await api.post("/v1/sessions/m-2/ingestions", json={"kind": "ingest"})
    assert submitted.status_code == 404
    assert await api.jobs.jobs_for("ingest_meeting") == []


async def test_forget_meeting_returns_the_c9_summary_with_its_stages(api: MemoryHarness) -> None:
    api.seed_twin("m-1")
    await api.status.record_phase(
        "m-1", PhaseUpdate("ended", STATUS_AT, terminal_reason="stopped_by_owner")
    )
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
        "removed",
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
    assert await api.store.load("m-1") is not None


async def test_forget_meeting_outside_the_principal_is_not_found(api: MemoryHarness) -> None:
    api.seed_session("m-2", owner_id="other_owner")

    response = await api.post("/v1/sessions/m-2/forget", json={})

    assert response.status_code == 404


async def test_forget_meeting_rejects_an_unreadable_keep_record(api: MemoryHarness) -> None:
    api.seed_twin("m-1")

    response = await api.post("/v1/sessions/m-1/forget", json={"keep_record": "maybe"})

    assert response.status_code == 400


async def test_reembedding_queues_a_job_in_postgres(api: MemoryHarness) -> None:
    api.seed_twin("m-1")

    response = await api.post("/v1/twins/demo_twin/reembeddings", json={})

    assert response.status_code == 202
    body = response.json()
    assert body["kind"] == "reembed"
    assert body["status"] == "queued"
    queued = await api.jobs.fetch(int(body["job_id"]))
    assert queued is not None
    assert queued.task == "reembed_twin"
    assert queued.args == {"owner_id": "demo_owner", "twin_id": "demo_twin"}

    polled = (await api.get(f"/v1/jobs/{body['job_id']}")).json()
    assert polled["kind"] == "reembed"
    assert polled["status"] == "queued"
    assert polled["completed"] is False


async def test_a_queued_job_of_another_owner_is_not_found(api: MemoryHarness) -> None:
    api.seed_twin("m-1")
    queued = await api.jobs.defer_reembed(owner_id="other_owner", twin_id="other_twin")

    response = await api.get(f"/v1/jobs/{queued}")

    assert response.status_code == 404


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
