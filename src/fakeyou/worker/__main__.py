import argparse
import asyncio
import importlib
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast

from fakeyou.config.settings import Settings
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.db.session_status import create_session_status
from fakeyou.orchestration.assembly import stop_signal_handlers
from fakeyou.orchestration.join_types import JoinPorts
from fakeyou.orchestration.providers import open_persona_memory
from fakeyou.orchestration.runtime import run_meeting_session
from fakeyou.transport.streams import StreamTransport
from fakeyou.worker.consumer import SnapshotStore, WorkerConsumer, WorkerDeps
from fakeyou.worker.debrief import DebriefConsumer, DebriefDeps
from fakeyou.worker.ingestion import IngestionConsumer, IngestionDeps
from fakeyou.worker.reembed import ReembedConsumer, ReembedDeps, TwinDirectory

SESSIONS_ROLE = "sessions"
INGESTION_ROLE = "ingestion"
DEBRIEF_ROLE = "debrief"
REEMBED_ROLE = "reembed"
ROLES = (SESSIONS_ROLE, INGESTION_ROLE, DEBRIEF_ROLE, REEMBED_ROLE)


class LongRunning(Protocol):
    async def run_forever(self) -> None: ...


class JobStore(SnapshotStore, TwinDirectory, Protocol): ...


def _session_store(record_dir: Path) -> JobStore:
    module: ModuleType = importlib.import_module("fakeyou.api.sessions")
    return cast(JobStore, module.SessionStore(record_dir))


async def verify_session_schema(settings: Settings) -> None:
    status = create_session_status(settings)
    try:
        await status.require_current()
    finally:
        await status.close()


async def run_consumers(*consumers: LongRunning) -> None:
    async with asyncio.TaskGroup() as group:
        for consumer in consumers:
            group.create_task(consumer.run_forever())


async def verify_boot_dependencies(settings: Settings, ports: JoinPorts) -> None:
    async with ports.open_memory(settings):
        return


async def serve(role: str) -> None:
    settings = Settings()
    ports = JoinPorts(
        run_session=run_meeting_session,
        open_memory=open_persona_memory,
        open_checkpointer=open_checkpointer,
    )
    await verify_boot_dependencies(settings, ports)
    if role == SESSIONS_ROLE:
        await verify_session_schema(settings)
    transport = StreamTransport.from_settings(settings)
    shutdown = asyncio.Event()
    async with AsyncExitStack() as exits:
        exits.push_async_callback(transport.aclose)
        shared = _Shared(
            settings=settings,
            transport=transport,
            store=_session_store(settings.paths.record_dir),
            ports=ports,
            shutdown=shutdown,
            worker_id=f"worker-{os.getpid()}",
            exits=exits,
        )
        consumers = _role_consumers(role, shared)
        with stop_signal_handlers(shutdown):
            await run_consumers(*consumers)


@dataclass(frozen=True, slots=True)
class _Shared:
    settings: Settings
    transport: StreamTransport
    store: JobStore
    ports: JoinPorts
    shutdown: asyncio.Event
    worker_id: str
    exits: AsyncExitStack


def role_consumers(role: str, shared: _Shared) -> list[LongRunning]:
    if role == SESSIONS_ROLE:
        status = create_session_status(shared.settings)
        shared.exits.push_async_callback(status.close)
        return [
            WorkerConsumer(
                WorkerDeps(
                    settings=shared.settings,
                    transport=shared.transport,
                    store=shared.store,
                    status=status,
                    ports=shared.ports,
                    shutdown=shared.shutdown,
                    worker_id=shared.worker_id,
                )
            )
        ]
    if role == INGESTION_ROLE:
        return [
            IngestionConsumer(
                IngestionDeps(
                    settings=shared.settings,
                    transport=shared.transport,
                    store=shared.store,
                    open_memory=open_persona_memory,
                    shutdown=shared.shutdown,
                    worker_id=shared.worker_id,
                )
            )
        ]
    if role == DEBRIEF_ROLE:
        return [
            DebriefConsumer(
                DebriefDeps(
                    settings=shared.settings,
                    transport=shared.transport,
                    store=shared.store,
                    shutdown=shared.shutdown,
                    worker_id=shared.worker_id,
                )
            )
        ]
    if role == REEMBED_ROLE:
        return [
            ReembedConsumer(
                ReembedDeps(
                    settings=shared.settings,
                    transport=shared.transport,
                    store=shared.store,
                    shutdown=shared.shutdown,
                    worker_id=shared.worker_id,
                )
            )
        ]
    raise ValueError(f"unknown worker role: {role!r}")


def _role_consumers(role: str, shared: _Shared) -> list[LongRunning]:
    return role_consumers(role, shared)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fakeyou.worker")
    parser.add_argument("role", choices=ROLES)
    arguments = parser.parse_args(argv)
    asyncio.run(serve(arguments.role))


if __name__ == "__main__":
    main()
