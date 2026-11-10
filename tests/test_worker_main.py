import asyncio
from collections.abc import Callable
from typing import Any, cast

import pytest
from procrastinate.testing import InMemoryConnector
from pydantic import SecretStr

import fakeyou.worker.__main__ as worker_main
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import GoogleReply, ReasoningEffort, Settings
from fakeyou.db.jobs import build_job_queue
from fakeyou.transport.streams import StreamTransport
from fakeyou.worker.consumer import WorkerConsumer
from fakeyou.worker.debrief import DebriefWorker
from fakeyou.worker.ingestion import IngestionWorker
from fakeyou.worker.reembed import ReembedWorker
from tests._session_status_fakes import MemorySessionStatus


class _ClosableTransport:
    async def aclose(self) -> None:
        return None


class _ClosableStatus(MemorySessionStatus):
    closed = 0

    async def close(self) -> None:
        type(self).closed += 1


def _models_ok(_role: str, _settings: Settings) -> None:
    return None


async def _started_roles(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    role_models: Callable[[str, Settings], None] = _models_ok,
) -> list[type[Any]]:
    started: list[type[Any]] = []

    async def capture(*consumers: Any) -> None:
        started.extend(type(consumer) for consumer in consumers)

    async def boot_ok(_settings: Settings, _ports: Any) -> None:
        return None

    monkeypatch.setattr(worker_main, "run_consumers", capture)
    monkeypatch.setattr(worker_main, "verify_boot_dependencies", boot_ok)
    monkeypatch.setattr(worker_main, "verify_role_models", role_models)

    async def schema_is_current(_settings: Settings) -> None:
        return None

    monkeypatch.setattr(worker_main, "verify_session_schema", schema_is_current)
    monkeypatch.setattr(worker_main, "create_session_status", lambda _settings: _ClosableStatus())
    monkeypatch.setattr(StreamTransport, "from_settings", lambda _settings: _ClosableTransport())
    monkeypatch.setattr(
        worker_main, "create_job_queue", lambda _settings: build_job_queue(InMemoryConnector())
    )
    await asyncio.wait_for(worker_main.serve(role), timeout=10)
    return started


@pytest.mark.usefixtures("settings")
async def test_sessions_role_runs_only_the_session_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_ClosableStatus, "closed", 0)

    assert await _started_roles(monkeypatch, "sessions") == [WorkerConsumer]
    assert _ClosableStatus.closed == 1


@pytest.mark.usefixtures("settings")
async def test_ingestion_role_runs_only_the_ingestion_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _started_roles(monkeypatch, "ingestion") == [IngestionWorker]


@pytest.mark.usefixtures("settings")
async def test_debrief_role_runs_only_the_debrief_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _started_roles(monkeypatch, "debrief") == [DebriefWorker]


@pytest.mark.usefixtures("settings")
async def test_reembed_role_runs_only_the_reembed_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _started_roles(monkeypatch, "reembed") == [ReembedWorker]


@pytest.mark.parametrize("role", ["sessions", "ingestion", "debrief", "reembed"])
async def test_no_role_runs_more_than_one_consumer(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    assert worker_main.ROLES == ("sessions", "ingestion", "debrief", "reembed")
    assert len(await _started_roles(monkeypatch, role)) == 1


def test_an_unknown_role_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown worker role"):
        worker_main.role_consumers("everything", cast(Any, None))


def test_the_entrypoint_requires_a_known_role() -> None:
    with pytest.raises(SystemExit):
        worker_main.main([])
    with pytest.raises(SystemExit):
        worker_main.main(["everything"])
    with pytest.raises(SystemExit):
        worker_main.main(["jobs"])


def _with_debrief(settings: Settings, reasoning_effort: ReasoningEffort | None) -> Settings:
    debrief = GoogleReply(
        model="gemini-3.8-flash",
        api_key=SecretStr("debrief-key"),
        reasoning_effort=reasoning_effort,
    )
    return settings.model_copy(update={"debrief": debrief})


def test_a_debrief_model_its_provider_cannot_run_is_refused_at_boot(settings: Settings) -> None:
    with pytest.raises(FakeYouError, match="does not support minimal reasoning effort"):
        worker_main.verify_role_models("debrief", _with_debrief(settings, "minimal"))


def test_a_debrief_model_with_a_supported_effort_passes_the_boot_check(
    settings: Settings,
) -> None:
    worker_main.verify_role_models("debrief", _with_debrief(settings, "low"))


@pytest.mark.usefixtures("settings")
async def test_the_worker_checks_its_role_models_before_it_consumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(role: str, _settings: Settings) -> None:
        raise FakeYouError(ErrorCode.CONFIG_INVALID, reason=f"{role} model")

    with pytest.raises(FakeYouError, match="debrief model"):
        await _started_roles(monkeypatch, "debrief", refuse)
