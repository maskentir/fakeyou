import shutil
import subprocess
import time
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
