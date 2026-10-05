import asyncio
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from itertools import count
from math import isfinite
from operator import itemgetter
from string import ascii_uppercase
from typing import Any, Literal

VOICE_ACTIVITY_DRAIN_JS = (
    "() => { const tracker = window.__fakeyouVoiceActivity;"
    " if (!tracker) return null; return tracker.drain(); }"
)
VOICE_PARTICIPANT_PREFIX = "speaker-"
PIPELINE_LAG_MS = 200.0
FRAME_LAG_MS = 300.0
ACTIVE_LEVEL = 0.01
DOMINANT_MARGIN = 0.5
MEET_AUDIO_SLOTS = 3
DEPARTURE_GRACE_MS = 3000.0
RETENTION_MS = 120_000.0
CLOCK_ANCHORS = 20
POLL_INTERVAL_SECONDS = 0.25

VoiceReason = Literal[
    "dominant_slot",
    "dominant_csrc",
    "no_identity",
    "csrc_conflict",
    "no_speech",
    "stale",
    "no_source",
    "overlap",
    "no_roster",
    "slot_contention",
    "slot_conflict",
    "self_overlap",
]
BinderRefusal = Literal["no_roster", "slot_contention", "slot_conflict", "self_overlap"]


def load_voice_activity_script() -> str:
    return (files("fakeyou") / "assets" / "voice_activity.js").read_text(encoding="utf-8")


def voice_label(index: int) -> str:
    letters = ""
    remaining = index + 1
    while remaining:
        remaining, position = divmod(remaining - 1, len(ascii_uppercase))
        letters = ascii_uppercase[position] + letters
    return letters


def voice_participant_id(label: str) -> str:
    return VOICE_PARTICIPANT_PREFIX + label.lower()


def voice_speaker_name(participant_id: str) -> str | None:
    label = participant_id.removeprefix(VOICE_PARTICIPANT_PREFIX)
    if label == participant_id or not label or not set(label) <= set(ascii_uppercase.lower()):
        return None
    return f"Speaker {label.upper()}"


@dataclass(slots=True)
class Tenure:
    label: str
    device: str | None = None


def voice_labels() -> Iterator[str]:
    return map(voice_label, count())


class SlotBinder:
    def __init__(self, labels: Iterator[str] | None = None) -> None:
        self._tenures: dict[int, Tenure] = {}
        self._present: frozenset[str] = frozenset()
        self._labels = labels if labels is not None else voice_labels()

    def update_roster(self, devices: Iterable[str]) -> None:
        present = frozenset(devices)
        for device in self._present - present:
            self._release(device)
        self._present = present
        if self._contended():
            self._tenures.clear()
        self._bind()

    def reset(self) -> None:
        self._tenures.clear()

    def resolve(self, slot: int, *, allow_new: bool = True) -> Tenure | BinderRefusal:
        if not self._present:
            return "no_roster"
        if self._contended():
            return "slot_contention"
        tenure = self._tenures.get(slot)
        if tenure is not None:
            return tenure
        if not allow_new:
            return "self_overlap"
        if len(self._unbound_tenures()) >= len(self._unbound_devices()):
            return "slot_conflict"
        tenure = Tenure(next(self._labels))
        self._tenures[slot] = tenure
        self._bind()
        return tenure

    def _contended(self) -> bool:
        return len(self._present) > MEET_AUDIO_SLOTS

    def _release(self, device: str) -> None:
        owned = [slot for slot, tenure in self._tenures.items() if tenure.device == device]
        released = owned or [
            slot for slot, tenure in self._tenures.items() if tenure.device is None
        ]
        for slot in released:
            del self._tenures[slot]

    def _unbound_devices(self) -> set[str]:
        return set(self._present) - {tenure.device for tenure in self._tenures.values()}

    def _unbound_tenures(self) -> list[Tenure]:
        return [tenure for tenure in self._tenures.values() if tenure.device is None]

    def _bind(self) -> None:
        devices = self._unbound_devices()
        tenures = self._unbound_tenures()
        if len(devices) == 1 and len(tenures) == 1:
            tenures[0].device = devices.pop()


@dataclass(frozen=True, slots=True)
class VoiceAttribution:
    participant_id: str
    reason: VoiceReason
    evidence_id: str
    slot: int | None = None
    bound: bool = False
    levels: tuple[tuple[int, float], ...] = ()
    csrc: tuple[int, ...] = ()

    def journal_fields(self) -> dict[str, Any]:
        return {
            "speaker_reason": self.reason,
            "speaker_slot": self.slot,
            "speaker_bound": self.bound,
            "speaker_levels": {str(slot): round(level, 4) for slot, level in self.levels},
            "speaker_csrc": list(self.csrc),
        }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if isfinite(value) else None


def _overlaps(spans: Sequence[tuple[float, float]], others: Iterable[tuple[float, float]]) -> bool:
    return any(
        start < other_end and other_start < end
        for start, end in spans
        for other_start, other_end in others
    )


class _Intervals:
    def __init__(self) -> None:
        self.closed: deque[tuple[float, float]] = deque()
        self.open_since: float | None = None

    def mark(self, active: bool, at_ms: float) -> None:
        if active and self.open_since is None:
            self.open_since = at_ms
        elif not active and self.open_since is not None:
            self.closed.append((self.open_since, at_ms))
            self.open_since = None

    def since(self, cursor_ms: float, now_ms: float) -> list[tuple[float, float]]:
        spans = [*self.closed]
        if self.open_since is not None:
            spans.append((self.open_since, now_ms))
        return [(max(start, cursor_ms), end) for start, end in spans if end > cursor_ms]

    def expire(self, cutoff_ms: float) -> None:
        while self.closed and self.closed[0][1] < cutoff_ms:
            self.closed.popleft()


class VoiceActivityTracker:
    def __init__(
        self,
        *,
        twin_name: str,
        monotonic: Callable[[], float] = time.monotonic,
        lag_ms: float = PIPELINE_LAG_MS,
        frame_lag_ms: float = FRAME_LAG_MS,
    ) -> None:
        self._twin_name = twin_name.strip().casefold()
        self._monotonic = monotonic
        self._lag_ms = lag_ms
        self._frame_lag_ms = frame_lag_ms
        self._levels: deque[tuple[float, int, float]] = deque()
        self._frames: deque[tuple[float, int, float, frozenset[int]]] = deque()
        self._partners: dict[int, set[int]] = {}
        self._csrc_labels: dict[int, str] = {}
        self._anchors: deque[tuple[float, float]] = deque(maxlen=CLOCK_ANCHORS)
        self._speech = _Intervals()
        self._self_speech = _Intervals()
        self._seen: dict[str, float] = {}
        self._local_slots: frozenset[int] = frozenset()
        self._sampled_page_ms: float | None = None
        self._instance: Any = None
        self._cursor_ms = 0.0
        self._attempts = 0
        self._labels = voice_labels()
        self._binder = SlotBinder(self._labels)

    def now_ms(self) -> float:
        return self._monotonic() * 1000.0

    def observe_vad(self, active: bool) -> None:
        self._speech.mark(active, self.now_ms())

    def observe_self_speech(self, active: bool) -> None:
        self._self_speech.mark(active, self.now_ms())

    def observe_batch(
        self, batch: Mapping[str, Any], *, sent_ms: float, received_ms: float
    ) -> None:
        page_ms = _number(batch.get("page_ms"))
        if page_ms is None:
            return
        instance = batch.get("instance")
        if instance != self._instance:
            if self._instance is not None:
                self._binder.reset()
                self._levels.clear()
                self._frames.clear()
            self._anchors.clear()
            self._instance = instance
        self._anchors.append((received_ms - sent_ms, (sent_ms + received_ms) / 2.0 - page_ms))
        self._sampled_page_ms = page_ms
        for entry in batch.get("levels") or ():
            if not isinstance(entry, Sequence) or len(entry) != 3:
                continue
            at, slot, level = (_number(value) for value in entry)
            if at is None or slot is None or level is None or not slot.is_integer():
                continue
            self._levels.append((at, int(slot), level))
        for entry in batch.get("frames") or ():
            self._observe_frame(entry)
        local = batch.get("local")
        if isinstance(local, list):
            self._local_slots = frozenset(int(slot) for slot in local if _number(slot) is not None)
        participants = batch.get("participants")
        if isinstance(participants, list):
            self._observe_roster(participants, received_ms)
        self._expire(received_ms)

    def attribute(self) -> VoiceAttribution:
        now = self.now_ms()
        spans = self._speech.since(self._cursor_ms, now)
        self._cursor_ms = now
        self._attempts += 1
        evidence_id = f"voice-{self._attempts}"
        if not spans:
            return VoiceAttribution("unknown", "no_speech", evidence_id)
        if self._partners:
            return self._attribute_csrc(spans, evidence_id)
        offset = self._offset()
        windows = [(start - self._lag_ms, end - self._lag_ms) for start, end in spans]
        if (
            offset is None
            or self._sampled_page_ms is None
            or self._sampled_page_ms + offset < windows[0][0]
        ):
            return VoiceAttribution("unknown", "stale", evidence_id)
        peaks: dict[int, float] = {}
        for at, slot, level in self._levels:
            mono = at + offset
            if slot not in self._local_slots and any(lo <= mono <= hi for lo, hi in windows):
                peaks[slot] = max(peaks.get(slot, 0.0), level)
        levels = tuple(sorted(peaks.items()))
        loud = {slot: peak for slot, peak in peaks.items() if peak >= ACTIVE_LEVEL}
        if not loud:
            covered = self._sampled_page_ms + offset >= windows[-1][1]
            reason: VoiceReason = "no_source" if covered else "stale"
            return VoiceAttribution("unknown", reason, evidence_id, levels=levels)
        ceiling = max(loud.values()) * DOMINANT_MARGIN
        dominant = sorted(slot for slot, peak in loud.items() if peak >= ceiling)
        if len(dominant) > 1:
            return VoiceAttribution("unknown", "overlap", evidence_id, levels=levels)
        slot = dominant[0]
        outcome = self._binder.resolve(
            slot, allow_new=not _overlaps(spans, self._self_speech.since(spans[0][0], now))
        )
        if isinstance(outcome, str):
            return VoiceAttribution("unknown", outcome, evidence_id, slot, levels=levels)
        return VoiceAttribution(
            voice_participant_id(outcome.label),
            "dominant_slot",
            evidence_id,
            slot,
            outcome.device is not None,
            levels,
        )

    def _attribute_csrc(
        self, spans: Sequence[tuple[float, float]], evidence_id: str
    ) -> VoiceAttribution:
        offset = self._offset()
        windows = [(start - self._frame_lag_ms, end - self._frame_lag_ms) for start, end in spans]
        if (
            offset is None
            or self._sampled_page_ms is None
            or self._sampled_page_ms + offset < windows[0][0]
        ):
            return VoiceAttribution("unknown", "stale", evidence_id)
        peaks: dict[int, float] = {}
        identities: dict[int, set[int]] = {}
        for at, slot, level, csrcs in self._frames:
            mono = at + offset
            if any(lo <= mono <= hi for lo, hi in windows):
                peaks[slot] = max(peaks.get(slot, 0.0), level)
                identities.setdefault(slot, set()).update(csrcs)
        levels = tuple(sorted(peaks.items()))
        loud = {slot: peak for slot, peak in peaks.items() if peak >= ACTIVE_LEVEL}
        if not loud:
            covered = self._sampled_page_ms + offset >= windows[-1][1]
            reason: VoiceReason = "no_source" if covered else "stale"
            return VoiceAttribution("unknown", reason, evidence_id, levels=levels)
        ceiling = max(loud.values()) * DOMINANT_MARGIN
        dominant = sorted(slot for slot, peak in loud.items() if peak >= ceiling)
        if len(dominant) > 1:
            return VoiceAttribution("unknown", "overlap", evidence_id, levels=levels)
        slot = dominant[0]
        markers = {csrc for csrc, partners in self._partners.items() if len(partners) > 1}
        identity = tuple(sorted(identities[slot] - markers))
        if not identity:
            return VoiceAttribution("unknown", "no_identity", evidence_id, slot, levels=levels)
        labeled = [csrc for csrc in identity if csrc in self._csrc_labels]
        known = {self._csrc_labels[csrc] for csrc in labeled}
        if len(known) > 1 or (labeled and len(labeled) < len(identity)):
            return VoiceAttribution(
                "unknown", "csrc_conflict", evidence_id, slot, levels=levels, csrc=identity
            )
        label = known.pop() if known else next(self._labels)
        for csrc in identity:
            self._csrc_labels.setdefault(csrc, label)
        return VoiceAttribution(
            voice_participant_id(label),
            "dominant_csrc",
            evidence_id,
            slot,
            levels=levels,
            csrc=identity,
        )

    def _observe_frame(self, entry: Any) -> None:
        if not isinstance(entry, Sequence) or len(entry) != 4:
            return
        at, slot, level = (_number(value) for value in entry[:3])
        sources = entry[3]
        if at is None or slot is None or level is None or not slot.is_integer():
            return
        if not isinstance(sources, Sequence):
            return
        csrcs = frozenset(
            int(value)
            for value in sources
            if (number := _number(value)) is not None and number.is_integer()
        )
        self._frames.append((at, int(slot), level, csrcs))
        for csrc in csrcs:
            self._partners.setdefault(csrc, set()).update(csrcs - {csrc})

    def _offset(self) -> float | None:
        if not self._anchors:
            return None
        return min(self._anchors, key=itemgetter(0))[1]

    def _observe_roster(self, participants: Sequence[Any], received_ms: float) -> None:
        for participant in participants:
            if not isinstance(participant, Mapping):
                continue
            device = participant.get("id")
            name = participant.get("name")
            if not isinstance(device, str) or not device:
                continue
            if isinstance(name, str) and name.strip().casefold() == self._twin_name:
                continue
            self._seen[device] = received_ms
        self._seen = {
            device: seen
            for device, seen in self._seen.items()
            if received_ms - seen <= DEPARTURE_GRACE_MS
        }
        self._binder.update_roster(self._seen)

    def _expire(self, now_ms: float) -> None:
        cutoff = now_ms - RETENTION_MS
        self._speech.expire(cutoff)
        self._self_speech.expire(cutoff)
        offset = self._offset()
        if offset is None:
            return
        while self._levels and self._levels[0][0] + offset < cutoff:
            self._levels.popleft()
        while self._frames and self._frames[0][0] + offset < cutoff:
            self._frames.popleft()


class VoiceActivityLoop:
    def __init__(
        self,
        page: Any,
        tracker: VoiceActivityTracker,
        *,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        self._page = page
        self._tracker = tracker
        self._poll_interval = poll_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def poll(self) -> bool:
        try:
            sent = self._tracker.now_ms()
            batch = await self._page.evaluate(VOICE_ACTIVITY_DRAIN_JS)
            received = self._tracker.now_ms()
            if not isinstance(batch, Mapping):
                return False
            self._tracker.observe_batch(batch, sent_ms=sent, received_ms=received)
            return True
        except Exception:
            return False

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

    async def _poll_loop(self) -> None:
        while True:
            await self.poll()
            await asyncio.sleep(self._poll_interval)
