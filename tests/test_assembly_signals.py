import asyncio
import os
import signal

import pytest

from fakeyou.orchestration.assembly import stop_signal_handlers


@pytest.mark.parametrize(
    ("first", "second", "expected_code"),
    [
        (signal.SIGINT, signal.SIGINT, 130),
        (signal.SIGTERM, signal.SIGTERM, 143),
        (signal.SIGTERM, signal.SIGINT, 130),
    ],
)
async def test_second_signal_forces_exit(
    first: signal.Signals, second: signal.Signals, expected_code: int
) -> None:
    stop = asyncio.Event()
    forced: list[int] = []
    released = asyncio.Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def force_exit(code: int) -> None:
        forced.append(code)
        released.set()

    with stop_signal_handlers(stop, force_exit=force_exit):
        os.kill(os.getpid(), first)
        await asyncio.wait_for(stop.wait(), 1)
        assert forced == []
        os.kill(os.getpid(), second)
        await asyncio.wait_for(released.wait(), 1)
        assert forced == [expected_code]

    assert {sig: signal.getsignal(sig) for sig in previous} == previous


async def test_signal_handlers_yield_installed_names() -> None:
    stop = asyncio.Event()

    with stop_signal_handlers(stop) as bound:
        assert bound.installed == ("SIGINT", "SIGTERM")
        assert bound.unavailable == ()


async def test_signal_handlers_report_unavailable_when_install_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoSignalsLoop:
        def add_signal_handler(self, *args: object, **kwargs: object) -> None:
            raise NotImplementedError("signals unavailable")

        def remove_signal_handler(self, *args: object) -> bool:
            del args
            return False

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _NoSignalsLoop())
    stop = asyncio.Event()

    with stop_signal_handlers(stop) as bound:
        assert bound.installed == ()
        assert bound.unavailable == ("SIGINT", "SIGTERM")
