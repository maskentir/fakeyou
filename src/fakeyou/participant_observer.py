import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from fakeyou.participants import Participant, SpeakerSample

POLL_INTERVAL_SECONDS = 0.25
TIMELINE_RETENTION_SECONDS = 120.0
QUERY_METHOD_MINING = "dom_mining"

OBSERVE_JS = """() => {
  const tiles = [];
  const seen = new Set();
  const push = (key, label, self, speaking) => {
    if (!key || seen.has(key)) return;
    seen.add(key);
    tiles.push({key, label, self, speaking});
  };
  try {
    const hinted = document.querySelectorAll(
      '[data-participant-id], [data-participant], [data-self]');
    for (const el of hinted) {
      const key = el.getAttribute('data-participant-id')
        || el.getAttribute('data-participant') || '';
      const label = (el.getAttribute('aria-label') || el.innerText || '')
        .slice(0, 120);
      const self = el.getAttribute('data-self') === 'true'
        || /\\bYou\\b/.test(label);
      const rawCls = el.className && el.className.baseVal !== undefined
        ? '' : el.className || '';
      const cls = String(rawCls);
      const speaking = /speaking/i.test(cls) || /speaking/i.test(label);
      push(key || ('tile#' + tiles.length), label, self, speaking);
    }
  } catch (e) {}
  let people = [];
  try {
    const panel = document.querySelector('[aria-label="People"]');
    const text = panel ? (panel.innerText || '') : '';
    people = text.split('\\n').map(s => s.slice(0, 120)).filter(s => s).slice(0, 40);
  } catch (e) {}
  let selfSeen = false;
  try {
    selfSeen = !!document.querySelector('[data-self="true"]');
  } catch (e) {}
  return {tiles, people, selfSeen};
}"""


class ParticipantObserver:
    def __init__(
        self,
        page: Any,
        *,
        epoch: str,
        monotonic: Callable[[], float] = time.monotonic,
        on_poll: Callable[[Mapping[str, Any], float], None] | None = None,
    ) -> None:
        self._page = page
        self._epoch = epoch
        self._monotonic = monotonic
        self._on_poll = on_poll
        self._samples: list[SpeakerSample] = []
        self._roster: dict[str, Participant] = {}
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def epoch(self) -> str:
        return self._epoch

    async def start(self) -> None:
        if self._task is not None or self._closed:
            return
        self._task = asyncio.create_task(self._poll_loop())

    async def close(self) -> None:
        task, self._task = self._task, None
        self._closed = True
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def reset_epoch(self, epoch: str) -> None:
        self._epoch = epoch
        self._samples = []
        self._roster = {}

    def snapshot(self) -> tuple[tuple[SpeakerSample, ...], Mapping[str, Participant]]:
        return tuple(self._samples), MappingProxyType(dict(self._roster))

    async def _poll_loop(self) -> None:
        while True:
            started = self._monotonic()
            try:
                raw = await self._page.evaluate(OBSERVE_JS)
                self._ingest(raw, started)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            elapsed = self._monotonic() - started
            await asyncio.sleep(max(0.0, POLL_INTERVAL_SECONDS - elapsed))

    def _ingest(self, raw: Any, at_seconds: float) -> None:
        if not isinstance(raw, dict):
            return
        tiles = raw.get("tiles")
        if not isinstance(tiles, list):
            return
        at_ms = at_seconds * 1000.0
        roster: dict[str, Participant] = {}
        active: list[str] = []
        for tile in tiles:
            if not isinstance(tile, dict):
                continue
            key = tile.get("key")
            if not isinstance(key, str) or not key:
                continue
            label = tile.get("label")
            roster[key] = Participant(
                key,
                label if isinstance(label, str) else "",
                self._epoch,
                bool(tile.get("self")),
            )
            if tile.get("speaking"):
                active.append(key)
        self._roster = roster
        self._samples.append(SpeakerSample(at_ms, self._epoch, tuple(active)))
        cutoff = at_ms - TIMELINE_RETENTION_SECONDS * 1000.0
        self._samples = [sample for sample in self._samples if sample.at_ms >= cutoff]
        if self._on_poll is not None:
            self._on_poll(raw, at_ms)


def describe_poll(raw: Mapping[str, Any]) -> dict[str, Any]:
    tiles = raw.get("tiles")
    names: Sequence[str] = []
    if isinstance(tiles, list):
        names = [
            str(tile.get("key", "")) for tile in tiles if isinstance(tile, dict) and tile.get("key")
        ]
    return {"method": QUERY_METHOD_MINING, "keys": list(names)}
