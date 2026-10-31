import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_clock: ContextVar[Callable[[], float]] = ContextVar("monotonic_clock", default=time.monotonic)


def monotonic() -> float:
    return _clock.get()()


@contextmanager
def using_clock(clock: Callable[[], float]) -> Iterator[None]:
    token = _clock.set(clock)
    try:
        yield
    finally:
        _clock.reset(token)
