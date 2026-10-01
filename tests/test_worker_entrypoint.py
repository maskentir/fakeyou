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
mode="${PACTL_MODE:-ready}"
if [ "$mode" = "notready" ]; then
  exit 1
fi
if [ "$1" = "info" ]; then
  echo "Default Sink: meeting_audio"
  if [ "$mode" = "wrong-source" ]; then
    echo "Default Source: alsa_input.usb"
  else
    echo "Default Source: agent_mic"
  fi
elif [ "$1" = "list" ]; then
  printf '0\\tmeeting_audio\\tmodule-null-sink.c\\ts16le 1ch 16000Hz\\tIDLE\\n'
  if [ "$mode" != "missing-sink" ]; then
    if [ "$mode" = "wrong-rate" ]; then
      spec="s16le 1ch 48000Hz"
    else
      spec="s16le 1ch 24000Hz"
    fi
    printf '1\\tagent_output\\tmodule-null-sink.c\\t%s\\tIDLE\\n' "$spec"
  fi
fi
exit 0
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

_GRACEFUL_CHILD = """#!/bin/sh
finish() {
  /bin/sleep 1
  touch "$STUB_LOG/child_finished"
  exit 0
}
trap finish TERM
touch "$STUB_LOG/child_started"
while :; do
  /bin/sleep 0.1
done
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


def test_not_ready_exits_infra_failure_and_cleans_up(tmp_path: Path) -> None:
    script, env = _stage(tmp_path, _CHILD, pactl_mode="notready", stub_sleep=True)
    result = subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 3
    assert "pulseaudio did not become ready" in result.stderr
    assert _kills(tmp_path) == 1


def test_wrong_topology_exits_infra_failure_and_cleans_up(tmp_path: Path) -> None:
    cases = (
        ("missing-sink", "expected PulseAudio sink 'agent_output' is missing"),
        ("wrong-rate", "PulseAudio sink 'agent_output' is not 24000Hz"),
        ("wrong-source", "default source is not agent_mic"),
    )
    for mode, message in cases:
        case = tmp_path / mode
        script, env = _stage(case, _CHILD, pactl_mode=mode, stub_sleep=False)
        result = subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=30)
        assert result.returncode == 3
        assert message in result.stderr
        assert _kills(case) == 1


def _start_and_signal(script: Path, env: dict[str, str], case: Path, sig: int) -> int:
    marker = case / "log/child_started"
    proc = subprocess.Popen([str(script)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert marker.exists()
        assert proc.poll() is None
        proc.send_signal(sig)
        return proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_signal_waits_for_graceful_child_shutdown(tmp_path: Path) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        case = tmp_path / str(int(sig))
        script, env = _stage(case, _GRACEFUL_CHILD, pactl_mode="ready", stub_sleep=False)

        status = _start_and_signal(script, env, case, sig)

        assert (case / "log/child_finished").exists()
        assert status == 0
        assert _kills(case) == 1


def test_signal_propagates_status_of_child_killed_by_forwarded_term(tmp_path: Path) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        case = tmp_path / str(int(sig))
        script, env = _stage(case, _SIGNAL_CHILD, pactl_mode="ready", stub_sleep=False)

        assert _start_and_signal(script, env, case, sig) == 143
        assert _kills(case) == 1


def test_start_and_exit_breadcrumbs_cover_early_failure(tmp_path: Path) -> None:
    script, env = _stage(tmp_path, _CHILD, pactl_mode="notready", stub_sleep=True)
    result = subprocess.run([str(script)], env=env, capture_output=True, text=True, timeout=30)
    assert "worker_start" in result.stderr
    assert '"exit_code":3' in result.stderr
    assert result.returncode == 3


def test_worker_exit_is_logged_without_command_arguments(tmp_path: Path) -> None:
    script, env = _stage(tmp_path, _CHILD, pactl_mode="ready", stub_sleep=False)
    env["CHILD_EXIT"] = "7"
    result = subprocess.run(
        [str(script), "sensitive-test-argument"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert '"exit_code":7' in result.stderr
    assert "sensitive-test-argument" not in result.stderr
    assert result.returncode == 7
