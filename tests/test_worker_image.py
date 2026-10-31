import shutil
import subprocess
import time
from typing import get_args
from uuid import uuid4

import pytest

from fakeyou.foundation.taxonomy import MeetingPhase

GRACEFUL_CHILD = (
    'trap "sleep 2; echo graceful-done; exit 0" TERM; echo child-started; '
    "while :; do sleep 0.2; done"
)


def _require_worker_image() -> None:
    if shutil.which("docker") is None:
        pytest.fail("docker binary is required for the worker image suite")
    inspected = subprocess.run(
        ["docker", "image", "inspect", "fakeyou-meeting-worker:local"],
        capture_output=True,
        check=False,
    )
    if inspected.returncode != 0:
        pytest.fail("build the meeting worker image before running the container suite")


@pytest.mark.container
def test_worker_image_ships_page_script_assets() -> None:
    _require_worker_image()
    probe = subprocess.run(
        [
            "docker",
            "compose",
            "run",
            "-T",
            "--rm",
            "--no-deps",
            "worker-sessions",
            "python",
            "-c",
            "from fakeyou.audio.voice_activity import load_voice_activity_script;"
            "from fakeyou.audio.stream_capture import load_capture_script;"
            "assert '__fakeyouVoiceActivity' in load_voice_activity_script();"
            "assert load_capture_script();"
            "print('page-assets-ok')",
        ],
        capture_output=True,
        text=True,
    )
    assert "page-assets-ok" in probe.stdout, probe.stdout + probe.stderr


@pytest.mark.container
def test_worker_image_waits_for_graceful_shutdown_after_sigint() -> None:
    _require_worker_image()
    name = f"fakeyou-signal-{uuid4().hex[:8]}"
    worker = subprocess.Popen(
        ["docker", "compose", "run", "-T", "--rm", "--no-deps", "--name", name]
        + ["worker-sessions", "sh", "-c", GRACEFUL_CHILD],
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
def test_worker_image_installs_the_postgres_checkpointer() -> None:
    _require_worker_image()
    probe = subprocess.run(
        [
            "docker",
            "compose",
            "run",
            "-T",
            "--rm",
            "--no-deps",
            "worker-sessions",
            "python",
            "-c",
            "from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver;"
            "from fakeyou.db.checkpoint import open_checkpointer;"
            "print('pg-checkpointer-ok')",
        ],
        capture_output=True,
        text=True,
    )
    assert "pg-checkpointer-ok" in probe.stdout, probe.stdout + probe.stderr


@pytest.mark.container
def test_worker_image_ships_the_parec_capture() -> None:
    _require_worker_image()
    probe = subprocess.run(
        [
            "docker",
            "compose",
            "run",
            "-T",
            "--rm",
            "--no-deps",
            "worker-sessions",
            "python",
            "-c",
            "import shutil;"
            "from fakeyou.audio.capture import ParecCapture;"
            "assert callable(ParecCapture.terminate);"
            "assert shutil.which('parec');"
            "print('parec-capture-ok')",
        ],
        capture_output=True,
        text=True,
    )
    assert "parec-capture-ok" in probe.stdout, probe.stdout + probe.stderr


def test_preparing_is_a_mapped_phase_name_and_never_a_public_one() -> None:
    from fakeyou.foundation.taxonomy import PublicPhase
    from fakeyou.orchestration.lifecycle import _TRANSITIONS

    produced = {transition.phase for transition in _TRANSITIONS.values()}

    assert "preparing" not in get_args(MeetingPhase)
    assert "preparing" not in get_args(PublicPhase)
    assert "preparing" not in produced
