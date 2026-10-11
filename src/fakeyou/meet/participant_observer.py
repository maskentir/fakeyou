import asyncio
import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from fakeyou.foundation.participants import Participant, SpeakerSample

POLL_INTERVAL_SECONDS = 0.25
TIMELINE_RETENTION_SECONDS = 120.0
QUERY_METHOD_MINING = "dom_mining"
EVIDENCE_FILE_NAME = "participants.jsonl"


def observer_epoch(meeting_id: str, generation: int = 1) -> str:
    return f"{meeting_id}:e{generation}"


def append_participant_evidence(
    path: Path,
    raw: Mapping[str, Any],
    mono_ms: float,
    *,
    epoch: str,
    method: str = QUERY_METHOD_MINING,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "wall": datetime.now(UTC).isoformat(),
        "mono_ms": mono_ms,
        "epoch": epoch,
        "method": method,
        "tiles": raw.get("tiles", []),
        "people": raw.get("people", []),
        "self_seen": bool(raw.get("selfSeen", False)),
        "page_speaking": raw.get("pageSpeaking", []),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


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


class ParticipantObserver:
    def __init__(
        self,
        page: Any,
        *,
        epoch: str,
        monotonic: Callable[[], float] = time.monotonic,
        on_poll: Callable[[Mapping[str, Any], float], None] | None = None,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._page = page
        self._epoch = epoch
        self._monotonic = monotonic
        self._on_poll = on_poll
        self._poll_interval = poll_interval_seconds
        self._samples: list[SpeakerSample] = []
        self._roster: dict[str, Participant] = {}
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.polls = 0
        self.poll_failures = 0
        self.last_poll_error: str | None = None

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
            self.polls += 1
            try:
                raw = await self._page.evaluate(OBSERVE_JS)
                self._ingest(raw, started)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.poll_failures += 1
                self.last_poll_error = type(error).__name__
            elapsed = self._monotonic() - started
            await asyncio.sleep(max(0.0, self._poll_interval - elapsed))

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
