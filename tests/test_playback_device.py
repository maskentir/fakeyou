import asyncio
import os
import stat
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import pytest

from fakeyou.audio.playback import PlaybackError, create_pactl_muter, create_paplay_stream_runner


async def _collect(chunks: Iterable[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def test_paplay_stream_runner_pipes_chunks_and_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "paplay"
    script.write_text(
        '#!/bin/sh\necho "$@" > "$FAKE_PAPLAY_ARGS"\ncat > "$FAKE_PAPLAY_STDIN"\nexit 0\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    args_file = tmp_path / "args"
    stdin_file = tmp_path / "stdin"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PAPLAY_ARGS", str(args_file))
    monkeypatch.setenv("FAKE_PAPLAY_STDIN", str(stdin_file))

    await create_paplay_stream_runner(sink="agent_output", rate=24000)(
        _collect([b"\x01", b"\x02\x03", b"\x04"])
    )

    assert args_file.read_text().split() == [
        "--device=agent_output",
        "--format=s16le",
        "--rate=24000",
        "--channels=1",
        "--raw",
    ]
    assert stdin_file.read_bytes() == b"\x01\x02\x03\x04"


async def test_paplay_stream_runner_broken_pipe_is_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "paplay"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    async def flood() -> AsyncIterator[bytes]:
        for _ in range(300):
            yield b"\x00" * 4096

    with pytest.raises(PlaybackError, match="paplay stream failed"):
        await create_paplay_stream_runner(sink="agent_output", rate=24000)(flood())


async def test_paplay_stream_runner_broken_pipe_during_cancel_is_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "paplay"
    script.write_text("#!/bin/sh\ncat > /dev/null\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    processes = []
    create_subprocess = asyncio.create_subprocess_exec

    async def capture_process(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await create_subprocess(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_process)

    async def fail_while_cancelling() -> AsyncIterator[bytes]:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        raise BrokenPipeError(32, "Broken pipe")
        yield b""

    try:
        with pytest.raises(asyncio.CancelledError):
            await create_paplay_stream_runner(sink="agent_output", rate=24000)(
                fail_while_cancelling()
            )
        assert processes[0].returncode is not None
        assert processes[0].stdin is not None
        assert processes[0].stdin.is_closing()
    finally:
        await processes[0].wait()


async def test_paplay_stream_runner_cleans_up_source_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "paplay"
    script.write_text('#!/bin/sh\necho $$ > "$FAKE_PAPLAY_PID"\nexec sleep 30\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PAPLAY_PID", str(pid_file))

    async def fail() -> AsyncIterator[bytes]:
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("paplay did not start")
        raise RuntimeError("source failed")
        yield b""

    with pytest.raises(RuntimeError, match="source failed"):
        await create_paplay_stream_runner(sink="agent_output", rate=24000)(fail())
    pid = int(pid_file.read_text())
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("paplay process survived source failure")


async def test_pactl_muter_reports_missing_binary() -> None:
    apply = create_pactl_muter(sink="agent_output")

    with pytest.raises(PlaybackError, match="pactl"):
        await apply(muted=True)


def test_pactl_muter_timeout_defaults_to_the_audio_setting() -> None:
    import inspect

    from fakeyou.config.settings import DEFAULTS

    parameters = inspect.signature(create_pactl_muter).parameters

    assert parameters["timeout_seconds"].default == DEFAULTS.audio.pactl_timeout_seconds


async def test_pactl_muter_toggles_sink_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "pactl"
    script.write_text('#!/bin/sh\necho "$@" >> "$FAKE_PACTL_CALLS"\nexit 0\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    calls = tmp_path / "calls"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PACTL_CALLS", str(calls))

    apply = create_pactl_muter(sink="agent_output")
    await apply(muted=True)
    await apply(muted=False)

    assert calls.read_text().splitlines() == [
        "set-sink-mute agent_output 1",
        "set-sink-mute agent_output 0",
    ]


async def test_pactl_muter_reaps_child_on_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "pactl"
    script.write_text('#!/bin/sh\necho $$ > "$FAKE_PACTL_PID"\nexec sleep 30\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PACTL_PID", str(pid_file))

    applying = asyncio.create_task(create_pactl_muter(sink="agent_output")(muted=True))
    for _ in range(500):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("pactl did not start")
    applying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await applying
    pid = int(pid_file.read_text())
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("pactl process survived cancellation")


async def test_pactl_muter_times_out_a_hung_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "pactl"
    script.write_text('#!/bin/sh\necho $$ > "$FAKE_PACTL_PID"\nexec sleep 30\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    pid_file = tmp_path / "pid"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PACTL_PID", str(pid_file))

    apply = create_pactl_muter(sink="agent_output", timeout_seconds=1.0)
    with pytest.raises(PlaybackError, match="timed out"):
        await apply(muted=True)

    for _ in range(500):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    pid = int(pid_file.read_text())
    for _ in range(500):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("pactl process survived its timeout")
