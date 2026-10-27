import asyncio
import json
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, override

from fakeyou.foundation.digest import canonical_json_digest
from fakeyou.foundation.invariants import POLL_TIMEOUT_MULTIPLIER
from fakeyou.foundation.polling import PageEvaluator, PollingLoop, PollSchedule
from fakeyou.observability.faults import ConsumerSink, Fault, FaultReporter, create_poll_guard

POLL_INTERVAL_SECONDS = 0.25
DEFAULT_PARTICIPANT_SCHEDULE = PollSchedule(interval_seconds=POLL_INTERVAL_SECONDS)
QUERY_METHOD_MINING = "dom_mining"


def observer_epoch(meeting_id: str, generation: int = 1) -> str:
    return f"{meeting_id}:e{generation}"


_DROPPED_TILE_FIELDS = frozenset({"html", "cls"})


def _evidence_record(
    raw: Mapping[str, Any], mono_ms: float, *, epoch: str, method: str
) -> dict[str, Any]:
    tiles: list[Any] = []
    raw_tiles = raw.get("tiles", [])
    if isinstance(raw_tiles, list):
        for tile in raw_tiles:
            if not isinstance(tile, dict):
                tiles.append(tile)
                continue
            tiles.append({k: v for k, v in tile.items() if k not in _DROPPED_TILE_FIELDS})
    return {
        "wall": datetime.now(UTC).isoformat(),
        "mono_ms": mono_ms,
        "epoch": epoch,
        "method": method,
        "tiles": tiles,
        "people": raw.get("people", []),
        "self_seen": bool(raw.get("selfSeen", False)),
        "page_speaking": raw.get("pageSpeaking", []),
    }


class ParticipantEvidenceWriter:
    def __init__(
        self,
        path: Path,
        *,
        epoch: str,
        method: str = QUERY_METHOD_MINING,
        max_bytes: int,
        faults: FaultReporter | None = None,
    ) -> None:
        self._path = path
        self._epoch = epoch
        self._method = method
        self._max_bytes = max_bytes
        self._faults = faults
        self._last_digest: str | None = None
        self._capped = False

    def write(self, raw: Mapping[str, Any], mono_ms: float) -> bool:
        record = _evidence_record(raw, mono_ms, epoch=self._epoch, method=self._method)
        content = {
            "tiles": record["tiles"],
            "people": record["people"],
            "self_seen": record["self_seen"],
            "page_speaking": record["page_speaking"],
        }
        digest = canonical_json_digest(content, ensure_ascii=False)
        if digest == self._last_digest:
            return False
        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        current = self._path.stat().st_size if self._path.exists() else 0
        if current + len(line) > self._max_bytes:
            self._record_capped()
            return False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("ab") as handle:
            handle.write(line)
        self._last_digest = digest
        return True

    def _record_capped(self) -> None:
        if self._capped:
            return
        self._capped = True
        if self._faults is None:
            return
        self._faults.record(
            Fault(
                source="browser",
                kind="unavailable",
                detail=f"participant evidence capped at {self._max_bytes} bytes",
            )
        )


OBSERVE_JS = """() => {
  const tiles = [];
  const seen = new Set();
  const push = (tile) => {
    if (!tile.key || seen.has(tile.key)) return;
    seen.add(tile.key);
    tiles.push(tile);
  };
  try {
    const hinted = document.querySelectorAll(
      '[data-participant-id], [data-participant], [data-self]');
    for (const el of hinted) {
      const key = el.getAttribute('data-participant-id')
        || el.getAttribute('data-participant') || '';
      const label = (el.getAttribute('aria-label') || '').slice(0, 120);
      const text = (el.innerText || '').slice(0, 200);
      const self = el.getAttribute('data-self') === 'true';
      const rawCls = el.className && el.className.baseVal !== undefined
        ? '' : el.className || '';
      const cls = String(rawCls).slice(0, 400);
      const kids = [];
      try {
        const labelled = el.querySelectorAll('[aria-label]');
        for (const kid of labelled) {
          kids.push((kid.getAttribute('aria-label') || '').slice(0, 120));
          if (kids.length >= 12) break;
        }
      } catch (e) {}
      const html = (el.outerHTML || '').slice(0, 3000);
      const speaking = /speaking/i.test(cls + ' ' + label + ' ' + kids.join(' '));
      push({key: key || ('tile#' + tiles.length), label, text,
        self, speaking, cls, kids, html});
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
  let pageSpeaking = [];
  try {
    const all = document.querySelectorAll('[aria-label]');
    for (const el of all) {
      const label = el.getAttribute('aria-label') || '';
      if (/speak/i.test(label)) pageSpeaking.push(label.slice(0, 160));
      if (pageSpeaking.length >= 20) break;
    }
  } catch (e) {}
  return {tiles, people, selfSeen, pageSpeaking};
}"""


def _is_observation(raw: Any) -> bool:
    return isinstance(raw, dict) and isinstance(raw.get("tiles"), list)


class ParticipantObserver(PollingLoop):
    _COMPENSATE_SLEEP = True

    def __init__(
        self,
        page: PageEvaluator,
        *,
        on_poll: Callable[[Mapping[str, Any], float], None],
        schedule: PollSchedule = DEFAULT_PARTICIPANT_SCHEDULE,
        sinks: ConsumerSink | None = None,
    ) -> None:
        super().__init__(
            poll_interval_seconds=schedule.interval_seconds,
            monotonic=schedule.monotonic,
            guard=create_poll_guard(sinks, source="browser", operation="participant_observer"),
        )
        self._page = page
        self._on_poll = on_poll
        self._sampled = asyncio.Event()

    @override
    async def start(self) -> None:
        await super().start()
        with suppress(TimeoutError):
            async with asyncio.timeout(POLL_TIMEOUT_MULTIPLIER * self._poll_interval):
                await self._sampled.wait()

    @override
    async def _poll_once(self) -> None:
        started = self._monotonic()
        self.polls += 1
        try:
            async with asyncio.timeout(POLL_TIMEOUT_MULTIPLIER * self._poll_interval):
                raw = await self._page.evaluate(OBSERVE_JS)
            if _is_observation(raw):
                await asyncio.to_thread(self._on_poll, raw, started * 1000.0)
        finally:
            self._sampled.set()
