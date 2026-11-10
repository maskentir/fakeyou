import argparse
import asyncio
import importlib
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import ModuleType
from typing import Protocol, cast

from fakeyou.config.settings import Settings
from fakeyou.db.jobs import create_job_queue
from fakeyou.db.session_status import SessionStatusStore, create_session_status
from fakeyou.orchestration.assembly import stop_signal_handlers
from fakeyou.orchestration.join_types import JoinPorts
from fakeyou.orchestration.providers import open_persona_memory, session_models
from fakeyou.orchestration.runtime import run_meeting_session
from fakeyou.persistence.segments import ObjectStore, create_object_store
from fakeyou.transport.streams import StreamTransport
from fakeyou.worker.consumer import SnapshotStore, WorkerConsumer, WorkerDeps
from fakeyou.worker.debrief import DebriefDeps, DebriefWorker, debrief_models
from fakeyou.worker.ingestion import IngestionDeps, IngestionWorker
from fakeyou.worker.launcher import LauncherDeps, SessionLauncher, create_session_jobs
from fakeyou.worker.reembed import ReembedDeps, ReembedWorker, TwinDirectory

SESSIONS_ROLE = "sessions"
LAUNCHER_ROLE = "launcher"
INGESTION_ROLE = "ingestion"
DEBRIEF_ROLE = "debrief"
REEMBED_ROLE = "reembed"
ROLES = (SESSIONS_ROLE, LAUNCHER_ROLE, INGESTION_ROLE, DEBRIEF_ROLE, REEMBED_ROLE)


class LongRunning(Protocol):
    async def run_forever(self) -> None: ...


class JobStore(SnapshotStore, TwinDirectory, Protocol): ...


def _session_store(status: SessionStatusStore) -> JobStore:
    module: ModuleType = importlib.import_module("fakeyou.api.sessions")
    return cast(JobStore, module.SessionStore(status))


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


def verify_role_models(role: str, settings: Settings) -> None:
    if role == SESSIONS_ROLE:
        session_models(settings)
    elif role == DEBRIEF_ROLE:
        debrief_models(settings)


async def serve(role: str, *, meeting_id: str | None = None) -> None:
    settings = Settings()
    ports = JoinPorts(
        run_session=run_meeting_session,
        open_memory=open_persona_memory,
    )
    verify_role_models(role, settings)
    await verify_boot_dependencies(settings, ports)
    if role in (SESSIONS_ROLE, LAUNCHER_ROLE):
        await verify_session_schema(settings)
    transport = StreamTransport.from_settings(settings)
    shutdown = asyncio.Event()
    async with AsyncExitStack() as exits:
        exits.push_async_callback(transport.aclose)
        status = create_session_status(settings)
        exits.push_async_callback(status.close)
        objects = create_object_store(settings)
        if objects is not None:
            exits.push_async_callback(objects.aclose)
        shared = _Shared(
            settings=settings,
            transport=transport,
            store=_session_store(status),
            status=status,
            objects=objects,
            ports=ports,
            shutdown=shutdown,
            worker_id=f"worker-{os.getpid()}",
            exits=exits,
        )
        consumers = _role_consumers(role, shared)
        with stop_signal_handlers(shutdown):
            if meeting_id is not None:
                consumer = consumers[0]
                assert isinstance(consumer, WorkerConsumer)
                claimed = await consumer.run_once(meeting_id=meeting_id)
                if claimed is not None:
                    await consumer.wait_session()
            else:
                await run_consumers(*consumers)


@dataclass(frozen=True, slots=True)
class _Shared:
    settings: Settings
    transport: StreamTransport
    store: JobStore
    status: SessionStatusStore
    objects: ObjectStore | None
    ports: JoinPorts
    shutdown: asyncio.Event
    worker_id: str
    exits: AsyncExitStack


def role_consumers(role: str, shared: _Shared) -> list[LongRunning]:
    if role == SESSIONS_ROLE:
        queue = create_job_queue(shared.settings)
        shared.exits.push_async_callback(queue.close)
        return [
            WorkerConsumer(
                WorkerDeps(
                    settings=shared.settings,
                    transport=shared.transport,
                    store=shared.store,
                    status=shared.status,
                    ports=shared.ports,
                    jobs=queue,
                    shutdown=shared.shutdown,
                    worker_id=shared.worker_id,
                    objects=shared.objects,
                )
            )
        ]
    if role == INGESTION_ROLE:
        queue = create_job_queue(shared.settings)
        shared.exits.push_async_callback(queue.close)
        return [
            IngestionWorker(
                IngestionDeps(
                    settings=shared.settings,
                    transport=shared.transport,
                    store=shared.store,
                    open_memory=open_persona_memory,
                    shutdown=shared.shutdown,
                ),
                queue,
            )
        ]
    if role == DEBRIEF_ROLE:
        queue = create_job_queue(shared.settings)
        shared.exits.push_async_callback(queue.close)
        return [
            DebriefWorker(
                DebriefDeps(
                    settings=shared.settings,
                    transport=shared.transport,
                    store=shared.store,
                    shutdown=shared.shutdown,
                ),
                queue,
            )
        ]
    if role == LAUNCHER_ROLE:
        return [
            SessionLauncher(
                LauncherDeps(
                    settings=shared.settings,
                    status=shared.status,
                    jobs=create_session_jobs(shared.settings),
                    shutdown=shared.shutdown,
                )
            )
        ]
    if role == REEMBED_ROLE:
        queue = create_job_queue(shared.settings)
        shared.exits.push_async_callback(queue.close)
        return [
            ReembedWorker(
                ReembedDeps(
                    settings=shared.settings,
                    transport=shared.transport,
                    store=shared.store,
                    shutdown=shared.shutdown,
                ),
                queue,
            )
        ]
    raise ValueError(f"unknown worker role: {role!r}")


def _role_consumers(role: str, shared: _Shared) -> list[LongRunning]:
    return role_consumers(role, shared)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="fakeyou.worker")
    parser.add_argument("role", choices=ROLES)
    parser.add_argument("--meeting", default=None)
    arguments = parser.parse_args(argv)
    if arguments.meeting is not None and arguments.role != SESSIONS_ROLE:
        parser.error("--meeting is only valid for the sessions role")
    asyncio.run(serve(arguments.role, meeting_id=arguments.meeting))


if __name__ == "__main__":
    main()
