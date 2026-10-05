import asyncio
import os
import shutil
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import pytest

GRACEFUL_CHILD = (
    'trap "sleep 2; echo graceful-done; exit 0" TERM; echo child-started; '
    "while :; do sleep 0.2; done"
)


def _worker_image_available() -> bool:
    if shutil.which("docker") is None:
        return False
    inspected = subprocess.run(
        ["docker", "image", "inspect", "fakeyou-meeting-worker:local"],
        capture_output=True,
        check=False,
    )
    return inspected.returncode == 0


@pytest.mark.container
@pytest.mark.skipif(not _worker_image_available(), reason="build the meeting worker image first")
def test_worker_image_waits_for_graceful_shutdown_after_sigint() -> None:
    name = f"fakeyou-signal-{uuid4().hex[:8]}"
    worker = subprocess.Popen(
        ["docker", "compose", "run", "-T", "--rm", "--no-deps", "--name", name]
        + ["meeting-worker", "sh", "-c", GRACEFUL_CHILD],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output: list[str] = []
    try:
        assert worker.stdout is not None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            line = worker.stdout.readline()
            if not line:
                break
            output.append(line)
            if "child-started" in line:
                subprocess.run(["docker", "kill", "--signal=SIGINT", name], check=True)
                break
        output.extend(worker.stdout)
        exit_code = worker.wait(timeout=60)
    finally:
        if worker.poll() is None:
            subprocess.run(["docker", "kill", name], check=False, capture_output=True)
            worker.wait(timeout=30)

    log = "".join(output)
    assert "graceful-done" in log, log
    assert '"exit_code":0' in log, log
    assert exit_code == 0


@pytest.mark.container
@pytest.mark.skipif(not _worker_image_available(), reason="build the meeting worker image first")
def test_worker_image_installs_the_postgres_checkpointer() -> None:
    probe = subprocess.run(
        [
            "docker",
            "compose",
            "run",
            "-T",
            "--rm",
            "--no-deps",
            "meeting-worker",
            "python",
            "-c",
            "from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver;"
            "from fakeyou.checkpoint import open_checkpointer;"
            "print('pg-checkpointer-ok')",
        ],
        capture_output=True,
        text=True,
    )
    assert "pg-checkpointer-ok" in probe.stdout, probe.stdout + probe.stderr


@pytest.mark.container
@pytest.mark.skipif(not _worker_image_available(), reason="build the meeting worker image first")
def test_compose_override_labels_are_queryable_by_meeting_id(tmp_path) -> None:
    from fakeyou.worker_backend import DockerWorkerBackend, compose_override

    meeting_id = f"probe-{uuid4().hex[:8]}"
    override = tmp_path / "override.yml"
    override.write_text(compose_override(meeting_id, role="worker"), encoding="utf-8")
    name = f"fakeyou-worker-{meeting_id}"
    try:
        started = subprocess.run(
            [
                "docker",
                "compose",
                "-f",
                "compose.yaml",
                "-f",
                str(override),
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "meeting-worker",
                "sh",
                "-c",
                "sleep 60",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert started.returncode == 0, started.stderr
        backend = DockerWorkerBackend(journal_dir=tmp_path / "journals")
        state = backend.status(meeting_id)
        assert state.container_state == "running"
        assert state.container_name == name
        assert state.container_id
        assert state.phase is None
    finally:
        subprocess.run(["docker", "stop", name], check=False, capture_output=True)


def _postgres_dsn() -> str:
    password = os.environ.get("POSTGRES_PASSWORD", "fakeyou-local-only")
    return f"postgresql://fakeyou:{password}@127.0.0.1:5432/fakeyou"


@pytest.mark.container
@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is required")
async def test_postgres_checkpointer_isolates_threads_and_reopens() -> None:
    from fakeyou.checkpoint import open_checkpointer
    from fakeyou.lifecycle import MeetingLifecycle, meeting_thread_id

    environment = {
        **os.environ,
        "FAKEYOU_MEMORY__STORE__PASSWORD": os.environ.get(
            "FAKEYOU_MEMORY__STORE__PASSWORD", "probe-only"
        ),
    }
    started = await asyncio.to_thread(
        subprocess.run,
        ["docker", "compose", "--profile", "production", "up", "-d", "postgres"],
        capture_output=True,
        text=True,
        env=environment,
    )
    assert started.returncode == 0, started.stderr
    dsn = _postgres_dsn()
    deadline = time.monotonic() + 300
    while True:
        try:
            async with open_checkpointer(Path("unused"), dsn=dsn):
                break
        except Exception as error:
            if time.monotonic() >= deadline:
                pytest.fail(f"postgres did not become ready: {error}")
            await asyncio.sleep(2)
    threads = [meeting_thread_id("pg-probe-twin", f"pg-probe-{name}") for name in ("a", "b")]
    try:
        async with open_checkpointer(Path("unused"), dsn=dsn) as saver:
            first = MeetingLifecycle("pg-probe-owner", "pg-probe-twin", "pg-probe-a", saver)
            await first.initialize()
            await first.send("join_requested", "owner_action", "probe")
            second = MeetingLifecycle("pg-probe-owner", "pg-probe-twin", "pg-probe-b", saver)
            await second.initialize()
            stored = [
                await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
                for thread_id in threads
            ]
            assert stored[0] is not None and stored[1] is not None
            assert stored[0].checkpoint["channel_values"]["phase"] == "lobby"
            assert stored[1].checkpoint["channel_values"]["phase"] == "preparing"
        async with open_checkpointer(Path("unused"), dsn=dsn) as saver:
            reopened = [
                await saver.aget_tuple({"configurable": {"thread_id": thread_id}})
                for thread_id in threads
            ]
            assert reopened[0] is not None and reopened[1] is not None
            assert reopened[0].checkpoint["channel_values"]["phase"] == "lobby"
            assert reopened[1].checkpoint["channel_values"]["phase"] == "preparing"
    finally:
        with suppress(Exception):
            async with open_checkpointer(Path("unused"), dsn=dsn) as saver:
                for thread_id in threads:
                    await saver.adelete_thread(thread_id)
