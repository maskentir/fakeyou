from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client.exceptions import ApiException

from fakeyou.config.settings import Settings
from fakeyou.db.session_status import SessionStatus, SessionStatusRepository
from fakeyou.observability.faults import capture_error
from fakeyou.worker.consumer import SESSION_LEASE_SECONDS
from fakeyou.worker.status_writer import StatusWriter

logger = logging.getLogger(__name__)

JOB_LABEL_APP = "fakeyou-session"
JOB_LABEL_MEETING = "fakeyou.dev/meeting-id"
_LAUNCHER_WORKER_ID = "launcher"


class SessionJobs(Protocol):
    async def active_meetings(self) -> set[str]: ...

    async def create(self, job: dict[str, Any]) -> bool: ...


class KubernetesSessionJobs:
    def __init__(self, settings: Settings) -> None:
        self._namespace = settings.sessions.namespace
        try:
            k8s_config.load_incluster_config()
        except k8s_config.ConfigException:
            k8s_config.load_kube_config()
        self._batch = k8s_client.BatchV1Api()

    async def active_meetings(self) -> set[str]:
        jobs = await asyncio.to_thread(
            self._batch.list_namespaced_job,
            self._namespace,
            label_selector=f"app={JOB_LABEL_APP}",
        )
        active: set[str] = set()
        for job in jobs.items:
            if job.status is not None and job.status.active:
                meeting_id = (job.metadata.labels or {}).get(JOB_LABEL_MEETING)
                if meeting_id:
                    active.add(meeting_id)
        return active

    async def create(self, job: dict[str, Any]) -> bool:
        try:
            await asyncio.to_thread(self._batch.create_namespaced_job, self._namespace, job)
        except ApiException as error:
            if error.status == 409:
                return False
            raise
        return True


def _job_name(meeting_id: str, attempts: int) -> str:
    cleaned = re.sub(r"[^a-z0-9-]", "-", meeting_id.lower()).strip("-")
    return f"session-{cleaned[:40]}-a{attempts + 1}"


def _deadline_seconds(settings: Settings, payload: Mapping[str, Any] | None) -> int:
    lobby = settings.meeting.lobby_timeout_seconds
    duration = settings.meeting.session_duration_seconds
    if payload is not None:
        if isinstance(payload.get("lobby_timeout"), (int, float)):
            lobby = float(payload["lobby_timeout"])
        if isinstance(payload.get("duration"), (int, float)):
            duration = float(payload["duration"])
    return math.ceil(lobby + duration) + settings.sessions.deadline_margin_seconds


def build_job(
    settings: Settings, status: SessionStatus, payload: Mapping[str, Any] | None
) -> dict[str, Any]:
    sessions = settings.sessions
    labels = {"app": JOB_LABEL_APP, JOB_LABEL_MEETING: status.meeting_id}
    env_from: list[dict[str, Any]] = []
    if sessions.env_from_secret:
        env_from.append({"secretRef": {"name": sessions.env_from_secret}})
    if sessions.env_from_configmap:
        env_from.append({"configMapRef": {"name": sessions.env_from_configmap}})
    container: dict[str, Any] = {
        "name": "session",
        "image": sessions.image,
        "command": ["python", "-m", "fakeyou.worker", "sessions", "--meeting", status.meeting_id],
        "resources": {
            "requests": {"cpu": sessions.cpu_request, "memory": sessions.memory_request},
            "limits": {"cpu": sessions.cpu_limit, "memory": sessions.memory_limit},
        },
    }
    if env_from:
        container["envFrom"] = env_from
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": _job_name(status.meeting_id, status.attempts), "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": sessions.job_ttl_seconds,
            "activeDeadlineSeconds": _deadline_seconds(settings, payload),
            "template": {
                "metadata": {"labels": labels},
                "spec": {"restartPolicy": "Never", "containers": [container]},
            },
        },
    }


@dataclass(frozen=True, slots=True)
class LauncherDeps:
    settings: Settings
    status: SessionStatusRepository
    jobs: SessionJobs
    shutdown: asyncio.Event


class SessionLauncher:
    def __init__(self, deps: LauncherDeps) -> None:
        self._deps = deps
        self._settings = deps.settings
        self._status = deps.status
        self._jobs = deps.jobs

    async def run_forever(self) -> None:
        while True:
            _launched, error = await capture_error(self.reconcile)
            if error is not None:
                logger.warning("session launcher reconcile failed: %s", error)
            if await self._sleep_or_closed(self._settings.sessions.poll_seconds):
                return

    async def _sleep_or_closed(self, seconds: float) -> bool:
        try:
            async with asyncio.timeout(seconds):
                await self._deps.shutdown.wait()
        except TimeoutError:
            return False
        return True

    async def reconcile(self) -> int:
        rows = await self._status.claimable()
        if not rows:
            return 0
        active = await self._jobs.active_meetings()
        launched = 0
        for row in rows:
            if row.desired_state == "stopped":
                await self._close_stopped(row.meeting_id)
            elif row.meeting_id not in active:
                payload = await self._status.payload(row.meeting_id)
                if await self._jobs.create(build_job(self._settings, row, payload)):
                    launched += 1
                    logger.info("launched session job for meeting %s", row.meeting_id)
        return launched

    async def _close_stopped(self, meeting_id: str) -> None:
        attempt_id = uuid4().hex
        claimed = await self._status.claim(
            _LAUNCHER_WORKER_ID, attempt_id, SESSION_LEASE_SECONDS, meeting_id=meeting_id
        )
        if claimed is None:
            return
        writer = StatusWriter(self._status, meeting_id, attempt_id=attempt_id)
        await writer.write(
            "ended",
            terminal_reason="stopped_by_owner",
            detail="stopped before the twin joined",
        )
        await writer.close()
        if writer.persisted == "ended":
            await self._status.release(meeting_id, _LAUNCHER_WORKER_ID, attempt_id)


def create_session_jobs(settings: Settings) -> SessionJobs:
    return KubernetesSessionJobs(settings)
