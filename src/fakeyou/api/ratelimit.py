from dataclasses import dataclass

from fakeyou.api.auth import Principal
from fakeyou.catalog.errors import ErrorCode, FakeYouError

_WINDOW_SECONDS = 60.0


@dataclass(slots=True)
class _Window:
    start: float
    count: int


class RateLimiter:
    def __init__(self, limit_per_minute: int) -> None:
        self._limit = limit_per_minute
        self._windows: dict[tuple[str, str | None], _Window] = {}

    def check(self, principal: Principal, *, at: float) -> None:
        key = (principal.kind, principal.owner_id)
        window = self._windows.get(key)
        if window is None or at - window.start >= _WINDOW_SECONDS:
            self._windows[key] = _Window(start=at, count=1)
            return
        window.count += 1
        if window.count > self._limit:
            raise FakeYouError(ErrorCode.API_RATE_LIMITED)
