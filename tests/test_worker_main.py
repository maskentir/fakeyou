import asyncio
from typing import Any, cast

import pytest

import fakeyou.worker.__main__ as worker_main
from fakeyou.config.settings import Settings
from fakeyou.transport.streams import StreamTransport
from fakeyou.worker.consumer import WorkerConsumer
from fakeyou.worker.debrief import DebriefConsumer
from fakeyou.worker.ingestion import IngestionConsumer
from fakeyou.worker.reembed import ReembedConsumer
from tests._session_status_fakes import MemorySessionStatus


class _ClosableTransport:
    async def aclose(self) -> None:
        return None


async def _started_roles(monkeypatch: pytest.MonkeyPatch, role: str) -> list[type[Any]]:
    started: list[type[Any]] = []

    async def capture(*consumers: Any) -> None:
        started.extend(type(consumer) for consumer in consumers)

    async def boot_ok(_settings: Settings, _ports: Any) -> None:
        return None

    monkeypatch.setattr(worker_main, "run_consumers", capture)
    monkeypatch.setattr(worker_main, "verify_boot_dependencies", boot_ok)

    async def schema_is_current(_settings: Settings) -> None:
        return None

    monkeypatch.setattr(worker_main, "verify_session_schema", schema_is_current)
    monkeypatch.setattr(
        worker_main, "create_session_status", lambda _settings: MemorySessionStatus()
    )
    monkeypatch.setattr(StreamTransport, "from_settings", lambda _settings: _ClosableTransport())
    await asyncio.wait_for(worker_main.serve(role), timeout=10)
    return started


@pytest.mark.usefixtures("settings")
async def test_sessions_role_runs_only_the_session_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _started_roles(monkeypatch, "sessions") == [WorkerConsumer]


@pytest.mark.usefixtures("settings")
async def test_ingestion_role_runs_only_the_ingestion_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _started_roles(monkeypatch, "ingestion") == [IngestionConsumer]


@pytest.mark.usefixtures("settings")
async def test_debrief_role_runs_only_the_debrief_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _started_roles(monkeypatch, "debrief") == [DebriefConsumer]


@pytest.mark.usefixtures("settings")
async def test_reembed_role_runs_only_the_reembed_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _started_roles(monkeypatch, "reembed") == [ReembedConsumer]


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
