import asyncio
import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, override

from playwright.async_api import Error as PlaywrightError

from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE, POLL_TIMEOUT_MULTIPLIER
from fakeyou.foundation.polling import PageEvaluator, PollGuard, PollingLoop
from fakeyou.foundation.resources import read_package_text

PROBE_DRAIN_JS = (
    "() => { const probe = window.__fakeyouAttributionProbe;"
    " if (!probe) return null; return probe.drain(); }"
)


def load_probe_script() -> str:
    return read_package_text("assets", "attribution_probe.js")


def append_probe_batch(path: Path, batch: Mapping[str, Any], *, mono_ms: float, wall: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {**batch, "kind": "page", "mono_ms": mono_ms, "wall": wall}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


class AttributionProbe:
    def __init__(
        self,
        path: Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], str] | None = None,
        sample_rate: int = CAPTURE_SAMPLE_RATE,
    ) -> None:
        self._path = path
        self._monotonic = monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC).isoformat())
        self._sample_rate = sample_rate
        self._samples = 0

    @property
    def audio_ms(self) -> float:
        return self._samples / self._sample_rate * 1000.0

    def monotonic(self) -> float:
        return self._monotonic()

    def observe_pcm(self, sample_count: int) -> None:
        if sample_count > 0:
            self._samples += sample_count

    def observe_vad(self, *, active: bool) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "kind": "vad",
            "active": active,
            "audio_ms": self.audio_ms,
            "mono_ms": self._monotonic() * 1000.0,
            "wall": self._wall_clock(),
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def append_page_batch(self, batch: Mapping[str, Any], *, mono_ms: float | None = None) -> None:
        append_probe_batch(
            self._path,
            batch,
            mono_ms=self._monotonic() * 1000.0 if mono_ms is None else mono_ms,
            wall=self._wall_clock(),
        )


class ProbeDrainLoop(PollingLoop):
    def __init__(
        self,
        page: PageEvaluator,
        probe: AttributionProbe,
        *,
        poll_interval_seconds: float = 1.0,
        guard: PollGuard | None = None,
    ) -> None:
        super().__init__(poll_interval_seconds=poll_interval_seconds, guard=guard)
        self._page = page
        self._probe = probe

    async def poll(self) -> bool:
        self.polls += 1
        try:
            before_ms = self._probe.monotonic() * 1000.0
            async with asyncio.timeout(POLL_TIMEOUT_MULTIPLIER * self._poll_interval):
                batch = await self._page.evaluate(PROBE_DRAIN_JS)
            after_ms = self._probe.monotonic() * 1000.0
            if not isinstance(batch, Mapping):
                self.poll_failures += 1
                return False
            self._probe.append_page_batch(batch, mono_ms=(before_ms + after_ms) / 2.0)
        except (OSError, PlaywrightError, TimeoutError) as error:
            self.poll_failures += 1
            self.last_poll_error = type(error).__name__
            return False
        else:
            return True

    @override
    async def _poll_once(self) -> None:
        await self.poll()
