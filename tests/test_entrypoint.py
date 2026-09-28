import os
import signal
import subprocess
import time
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker/audio/entrypoint.sh"

_PULSEAUDIO = """#!/bin/sh
for arg in "$@"; do
  if [ "$arg" = "--kill" ]; then
    echo kill >> "$STUB_LOG/kills"
  fi
done
exit 0
"""

_PACTL = """#!/bin/sh
if [ "${PACTL_MODE:-ready}" = "ready" ]; then
  exit 0
else
  exit 1
fi
"""

_SLEEP = """#!/bin/sh
exit 0
"""

_CHILD = """#!/bin/sh
exit "${CHILD_EXIT:-0}"
"""

_SIGNAL_CHILD = """#!/bin/sh
touch "$STUB_LOG/child_started"
exec /bin/sleep 30
"""


def _stage(
    tmp_path: Path, child: str, *, pactl_mode: str, stub_sleep: bool
) -> tuple[Path, dict[str, str]]:
    stub = tmp_path / "stub"
    log = tmp_path / "log"
    runtime = tmp_path / "runtime"
    stub.mkdir(parents=True)
    log.mkdir(parents=True)
    runtime.mkdir(parents=True)
    for name, body in (("pulseaudio", _PULSEAUDIO), ("pactl", _PACTL), ("child.sh", child)):
        path = stub / name
        path.write_text(body)
        path.chmod(0o755)
    if stub_sleep:
        path = stub / "sleep"
        path.write_text(_SLEEP)
        path.chmod(0o755)
    script = tmp_path / "fakeyou-entrypoint"
    script.write_text(ENTRYPOINT.read_text().replace("/entrypoint.sh", str(stub / "child.sh")))
    script.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(log),
        "XDG_RUNTIME_DIR": str(runtime),
        "PACTL_MODE": pactl_mode,
    }
    return script, env


def _kills(case: Path) -> int:
    kills = case / "log/kills"
    return kills.read_text().count("kill") if kills.exists() else 0


def test_syntax_check_passes() -> None:
    result = subprocess.run(["sh", "-n", str(ENTRYPOINT)], capture_output=True, text=True)
    assert result.returncode == 0


def test_child_exit_propagates_and_pulseaudio_killed(tmp_path: Path) -> None:
    for child_exit, expected in (("0", 0), ("3", 3)):
        case = tmp_path / child_exit
        script, env = _stage(case, _CHILD, pactl_mode="ready", stub_sleep=False)
        env["CHILD_EXIT"] = child_exit
        result = subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == expected
        assert _kills(case) == 1


def test_not_ready_exits_1_without_kill(tmp_path: Path) -> None:
    script, env = _stage(tmp_path, _CHILD, pactl_mode="notready", stub_sleep=True)
    result = subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "pulseaudio did not become ready" in result.stderr
    assert _kills(tmp_path) == 0


def test_signal_forwards_to_child_and_exits_128_plus_signo(tmp_path: Path) -> None:
    for sig, expected in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
        case = tmp_path / str(int(sig))
        script, env = _stage(case, _SIGNAL_CHILD, pactl_mode="ready", stub_sleep=False)
        marker = case / "log/child_started"
        proc = subprocess.Popen(
            [str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
            assert marker.exists()
            assert proc.poll() is None
            proc.send_signal(sig)
            assert proc.wait(timeout=10) == expected
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        assert _kills(case) == 1
