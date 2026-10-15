import asyncio
from typing import Any

import pytest

from fakeyou.audio.capture import CAPTURE_CHUNK_BYTES, CaptureError, ParecCapture


class _FakeProcess:
    def __init__(
        self, stdout: bytes, *, returncode: int = 0, stderr: bytes = b"", eof: bool = True
    ) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(stdout)
        if eof:
            self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.returncode: int | None = None
        self._exit_code = returncode
        self.terminated = False

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


def _spawn(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> list[tuple[Any, ...]]:
    argv: list[tuple[Any, ...]] = []

    async def fake_exec(*args: Any, **_kwargs: Any) -> _FakeProcess:
        argv.append(args)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return argv


def _ignore(error: CaptureError) -> None:
    del error


async def _settled(failures: list[CaptureError]) -> None:
    for _ in range(200):
        if failures:
            return
        await asyncio.sleep(0.01)


async def test_parec_is_started_for_raw_16khz_mono_s16le(monkeypatch: pytest.MonkeyPatch) -> None:
    argv = _spawn(monkeypatch, _FakeProcess(b"", eof=False))
    capture = ParecCapture(on_failure=_ignore)

    await capture.start()
    capture.terminate()

    assert argv == [("parec", "--format=s16le", "--rate=16000", "--channels=1", "--raw")]


async def test_captured_bytes_arrive_in_fixed_chunks_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = bytes(range(256)) * 20
    _spawn(monkeypatch, _FakeProcess(payload, eof=False))
    capture = ParecCapture(on_failure=_ignore)

    await capture.start()
    first = await capture.queue.get(wait_seconds=1)
    second = await capture.queue.get(wait_seconds=1)
    capture.terminate()

    assert first == payload[:CAPTURE_CHUNK_BYTES]
    assert second == payload[CAPTURE_CHUNK_BYTES : 2 * CAPTURE_CHUNK_BYTES]


async def test_unexpected_exit_is_reported_with_the_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _spawn(monkeypatch, _FakeProcess(b"\x01\x00" * 10, returncode=1, stderr=b"no such source"))
    failures: list[CaptureError] = []
    capture = ParecCapture(on_failure=failures.append)

    await capture.start()
    await asyncio.wait_for(_settled(failures), timeout=2)

    assert len(failures) == 1
    assert "exited with 1" in str(failures[0])
    assert "no such source" in str(failures[0])


async def test_clean_terminate_is_not_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(b"", eof=False)
    _spawn(monkeypatch, process)
    failures: list[CaptureError] = []
    capture = ParecCapture(on_failure=failures.append)

    await capture.start()
    capture.terminate()
    await asyncio.sleep(0.01)

    assert process.terminated is True
    assert failures == []


async def test_missing_parec_binary_fails_start_with_a_capture_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def missing(*_args: Any, **_kwargs: Any) -> _FakeProcess:
        raise FileNotFoundError("parec")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    capture = ParecCapture()

    with pytest.raises(CaptureError, match="parec is unavailable"):
        await capture.start()
