from __future__ import annotations

from pathlib import Path
from typing import Any

LINK = "https://meet.google.com/abc-defg-hij"
DATABASE_URL = "postgresql://fakeyou:test@localhost:5432/fakeyou"
STREAM_CAPTURE_TEST_FLAG = "window.__fakeyouTestHooks = true;"


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def env_file_opt(path: Path | str | None) -> dict[str, Any]:
    return {"_env_file": path}
