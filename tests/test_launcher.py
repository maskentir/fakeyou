import asyncio
from datetime import UTC, datetime
from typing import Any

from fakeyou.config.settings import Settings
from fakeyou.db.session_status import SessionStatus
from fakeyou.worker.launcher import LauncherDeps, SessionLauncher, build_job
from tests._session_status_fakes import MemorySessionStatus

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


class FakeJobs:
    def __init__(self, active: set[str] | None = None) -> None:
        self.active = active or set()
        self.created: list[dict[str, Any]] = []

    async def active_meetings(self) -> set[str]:
        return set(self.active)

    async def create(self, job: dict[str, Any]) -> bool:
        self.created.append(job)
        return True


def _status(meeting_id: str, attempts: int = 0) -> SessionStatus:
    return SessionStatus(
        meeting_id=meeting_id,
        owner_id="owner-1",
        desired_state="joined",
        phase="created",
        terminal_reason=None,
        detail=None,
        attempt_id=None,
        created_at=NOW,
        updated_at=NOW,
        attempts=attempts,
    )


def test_the_job_runs_one_meeting_with_no_retries_and_a_deadline(settings: Settings) -> None:
    settings.sessions.image = "fakeyou-worker:test"
    settings.sessions.env_from_secret = "fakeyou-secrets"

    job = build_job(
        settings,
        _status("m_1", attempts=2),
        {
            "lobby_timeout": 60.0,
            "duration": 3600.0,
            "trace_id": "4bf92f3577b34da6a3ce929d0e0e4736",
        },
    )

    assert job["metadata"]["name"] == "session-m-1-a3"
    assert job["metadata"]["labels"]["fakeyou.dev/meeting-id"] == "m_1"
    spec = job["spec"]
    assert spec["backoffLimit"] == 0
    assert spec["ttlSecondsAfterFinished"] == 3600
    assert spec["activeDeadlineSeconds"] == 60 + 3600 + 600
    container = spec["template"]["spec"]["containers"][0]
    assert container["image"] == "fakeyou-worker:test"
    assert container["command"] == [
        "python",
        "-m",
        "fakeyou.worker",
        "sessions",
        "--meeting",
        "m_1",
    ]
    assert container["envFrom"] == [{"secretRef": {"name": "fakeyou-secrets"}}]
    [trace_env] = container["env"]
    assert trace_env["name"] == "TRACEPARENT"
    assert trace_env["value"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    assert spec["template"]["spec"]["restartPolicy"] == "Never"


def test_the_deadline_falls_back_to_meeting_defaults_without_a_payload(
    settings: Settings,
) -> None:
    job = build_job(settings, _status("m-1"), None)

    expected = 300 + 7200 + settings.sessions.deadline_margin_seconds
    assert job["spec"]["activeDeadlineSeconds"] == expected


async def _seeded_launcher(
    settings: Settings, jobs: FakeJobs
) -> tuple[SessionLauncher, MemorySessionStatus]:
    status = MemorySessionStatus()
    await status.create("m-1", "owner-1", NOW, payload={"meeting_id": "m-1"})
    await status.desire("m-1", "joined", NOW)
    launcher = SessionLauncher(
        LauncherDeps(settings=settings, status=status, jobs=jobs, shutdown=asyncio.Event())
    )
    return launcher, status


async def test_reconcile_launches_a_job_for_a_waiting_session(settings: Settings) -> None:
    jobs = FakeJobs()
    launcher, _status_row = await _seeded_launcher(settings, jobs)

    assert await launcher.reconcile() == 1
    assert [job["metadata"]["name"] for job in jobs.created] == ["session-m-1-a1"]


async def test_reconcile_skips_sessions_with_an_active_job(settings: Settings) -> None:
    jobs = FakeJobs(active={"m-1"})
    launcher, _status_row = await _seeded_launcher(settings, jobs)

    assert await launcher.reconcile() == 0
    assert jobs.created == []


async def test_reconcile_closes_a_session_stopped_before_it_ever_joined(
    settings: Settings,
) -> None:
    jobs = FakeJobs()
    launcher, status = await _seeded_launcher(settings, jobs)
    await status.desire("m-1", "stopped", NOW)

    assert await launcher.reconcile() == 0

    closed = await status.get("m-1")
    assert closed is not None
    assert (closed.phase, closed.terminal_reason) == ("ended", "stopped_by_owner")
    assert jobs.created == []
