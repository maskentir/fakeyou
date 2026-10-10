from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import count
from string import ascii_uppercase
from typing import TYPE_CHECKING, Any, Literal
from uuid import NAMESPACE_URL, uuid5

if TYPE_CHECKING:
    from fakeyou.identity.voiceprint_match import VoiceScores

ResolveRefusal = Literal["no_roster", "slot_contention", "slot_conflict", "self_overlap"]
VerifyResult = Literal["bound", "repeat", "conflict", "unknown"]

SPEAKER_EVENT_BUFFER = 128


def _speaker_label(index: int) -> str:
    letters = ""
    remaining = index + 1
    while remaining:
        remaining, position = divmod(remaining - 1, len(ascii_uppercase))
        letters = ascii_uppercase[position] + letters
    return letters


def _speaker_labels() -> Iterator[str]:
    return map(_speaker_label, count())


def _speaker_uuid(label: str) -> str:
    return uuid5(NAMESPACE_URL, "speaker:" + label).hex


@dataclass(frozen=True, slots=True)
class SpeakerRow:
    uuid: str
    label: str
    device: str | None
    status: str
    confidence: float
    margin: float
    slot: int | None
    csrcs: tuple[int, ...]
    track: str
    level: float
    history: tuple[str, ...]
    display_claim: str | None = None
    claim_confidence: float = 0.0
    verified_name: str | None = None
    verified_enrollment: str | None = None


@dataclass(slots=True)
class _Record:
    label: str
    device: str | None = None
    slot: int | None = None
    display_claim: str | None = None
    claim_confidence: float = 0.0
    claim_locked: bool = False
    verified_name: str | None = None
    verified_enrollment: str | None = None
    verified_locked: bool = False
    followed: bool = False
    history: list[str] = field(default_factory=list)


class SpeakerRegistry:
    def __init__(self, labels: Iterator[str] | None = None, capacity: int = 3) -> None:
        self._rows: dict[str, _Record] = {}
        self._by_slot: dict[int, str] = {}
        self._by_csrc: dict[int, str] = {}
        self._stream_csrcs: dict[int, frozenset[int]] = {}
        self._stream_meta: dict[int, tuple[str, float]] = {}
        self._present: frozenset[str] = frozenset()
        self._dormant: dict[str, tuple[int, str]] = {}
        self._labels = labels if labels is not None else _speaker_labels()
        self._capacity = capacity
        self._events: list[dict[str, Any]] = []
        self._slot_labels: dict[int, str] = {}
        self._voice_scores: dict[str, VoiceScores] = {}
        self._pending_claims: dict[str, str] = {}

    @property
    def roster_size(self) -> int:
        return len(self._present)

    def drain_events(self) -> list[dict[str, Any]]:
        drained = self._events
        self._events = []
        return drained

    def stream_csrcs(self, slot: int) -> tuple[int, ...]:
        return tuple(sorted(self._stream_csrcs.get(slot, frozenset())))

    def update_roster(self, devices: Iterable[str]) -> None:
        present = frozenset(devices)
        for device in self._present - present:
            self._release(device)
        self._present = present
        if self._contended():
            self._dormant.clear()
        else:
            for device in present:
                saved = self._dormant.pop(device, None)
                if saved is None:
                    continue
                if saved[0] in self._by_slot:
                    self._dormant[device] = saved
                    continue
                self._by_slot[saved[0]] = saved[1]
                record = self._rows[saved[1]]
                record.slot = saved[0]
                record.history.append("restored")
                self._emit(
                    {
                        "event": "tenure_restored",
                        "label": record.label,
                        "slot": saved[0],
                        "device": device,
                    }
                )
        self._bind()

    def reset(self) -> None:
        for uuid in self._by_slot.values():
            self._rows[uuid].slot = None
        for record in self._rows.values():
            record.verified_name = None
            record.verified_enrollment = None
            record.verified_locked = False
        self._by_slot.clear()
        self._stream_csrcs.clear()
        self._stream_meta.clear()
        self._dormant.clear()
        self._slot_labels.clear()
        self._events.clear()

    def observe_stream(
        self, slot: int, csrcs: Sequence[int], *, track: str = "", level: float = 0.0
    ) -> None:
        self._stream_csrcs[slot] = frozenset(csrcs)
        self._stream_meta[slot] = (track, level)

    def resolve(self, slot: int, *, allow_new: bool = True) -> SpeakerRow | ResolveRefusal:
        if not self._present:
            return "no_roster"
        uuid = self._by_slot.get(slot)
        if uuid is not None:
            self._remember(slot, self._rows[uuid])
            self._index_slot(slot, uuid)
            return self._snapshot(uuid)
        if self._demand_full():
            return "slot_contention"
        followed = self._follow_csrc(slot)
        if followed is not None:
            self._remember(slot, self._rows[followed])
            self._index_slot(slot, followed)
            return self._snapshot(followed)
        if not allow_new:
            return "self_overlap"
        if len(self._unbound_rows()) >= len(self._unbound_devices()):
            return "slot_conflict"
        uuid = self._mint(slot)
        self._bind()
        self._remember(slot, self._rows[uuid])
        self._index_slot(slot, uuid)
        return self._snapshot(uuid)

    def resolve_csrcs(self, csrcs: frozenset[int]) -> SpeakerRow | None:
        labeled = [csrc for csrc in csrcs if csrc in self._by_csrc]
        known = {self._by_csrc[csrc] for csrc in labeled}
        if len(known) > 1 or (labeled and len(labeled) < len(csrcs)):
            return None
        uuid = known.pop() if known else self._mint(None)
        for csrc in csrcs:
            self._by_csrc.setdefault(csrc, uuid)
        return self._snapshot(uuid)

    def note_voice_scores(self, uuid: str, scores: VoiceScores) -> None:
        if uuid not in self._rows:
            return
        self._voice_scores[uuid] = scores

    def row(self, uuid: str) -> SpeakerRow | None:
        if uuid not in self._rows:
            return None
        return self._snapshot(uuid)

    def note_roster(self, entries: Iterable[Mapping[str, str]]) -> None:
        seen: dict[str, str] = {}
        for entry in entries:
            seen[entry["id"]] = entry["name"]
        self._pending_claims.update(seen)
        for device, name in seen.items():
            self._apply_claim(device, name)

    def _claim_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for name in self._pending_claims.values():
            counts[name] = counts.get(name, 0) + 1
        return counts

    def _apply_claim(self, device: str, name: str) -> None:
        uuid = next(
            (known for known, record in self._rows.items() if record.device == device),
            None,
        )
        if uuid is None:
            return
        record = self._rows[uuid]
        if self._claim_counts().get(name, 0) > 1:
            record.display_claim = None
            record.claim_confidence = 0.0
            return
        if record.claim_locked:
            if record.display_claim is not None and name != record.display_claim:
                record.display_claim = None
                record.claim_confidence = 0.0
                self._emit(
                    {
                        "event": "display_claim_changed",
                        "label": record.label,
                        "slot": record.slot,
                        "device": device,
                    }
                )
            return
        record.display_claim = name
        record.claim_confidence = 1.0
        record.claim_locked = True

    def note_verified(self, uuid: str, person_id: str, name: str) -> VerifyResult:
        record = self._rows.get(uuid)
        if record is None:
            return "unknown"
        if record.verified_locked:
            if record.verified_enrollment == person_id and record.verified_name == name:
                return "repeat"
            return "conflict"
        record.verified_name = name
        record.verified_enrollment = person_id
        record.verified_locked = True
        return "bound"

    def _emit(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        while len(self._events) > SPEAKER_EVENT_BUFFER:
            del self._events[0]

    def _remember(self, slot: int, record: _Record) -> None:
        previous = self._slot_labels.get(slot)
        if previous is not None and previous != record.label:
            self._emit(
                {
                    "event": "tenure_relabel",
                    "slot": slot,
                    "old_label": previous,
                    "new_label": record.label,
                }
            )
        self._slot_labels[slot] = record.label

    def _mint(self, slot: int | None) -> str:
        label = next(self._labels)
        uuid = _speaker_uuid(label)
        self._rows[uuid] = _Record(label=label, slot=slot, history=["minted"])
        if slot is not None:
            self._by_slot[slot] = uuid
            self._emit({"event": "tenure_minted", "label": label, "slot": slot})
        return uuid

    def _index_slot(self, slot: int, uuid: str) -> None:
        for csrc in self._stream_csrcs.get(slot, frozenset()):
            self._by_csrc[csrc] = uuid

    def _snapshot(self, uuid: str) -> SpeakerRow:
        record = self._rows[uuid]
        if record.device is not None:
            status = "confirmed"
            confidence = 1.0
        elif record.followed:
            status = "provisional"
            confidence = 0.7
        else:
            status = "provisional"
            confidence = 0.5
        if record.slot is None:
            csrcs = tuple(sorted(csrc for csrc, owner in self._by_csrc.items() if owner == uuid))
            track = ""
            level = 0.0
        else:
            csrcs = tuple(sorted(self._stream_csrcs.get(record.slot, frozenset())))
            track, level = self._stream_meta.get(record.slot, ("", 0.0))
        scores = self._voice_scores.get(uuid)
        margin = scores.s1 - scores.s2 if scores is not None else 1.0
        return SpeakerRow(
            uuid=uuid,
            label=record.label,
            device=record.device,
            status=status,
            confidence=confidence,
            margin=margin,
            slot=record.slot,
            csrcs=csrcs,
            track=track,
            level=level,
            history=tuple(record.history),
            display_claim=record.display_claim,
            claim_confidence=record.claim_confidence,
            verified_name=record.verified_name,
            verified_enrollment=record.verified_enrollment,
        )

    def _follow_csrc(self, slot: int) -> str | None:
        csrcs = self._stream_csrcs.get(slot, frozenset())
        if not csrcs:
            return None
        matches = {
            other
            for other in self._by_slot
            if other != slot and self._stream_csrcs.get(other, frozenset()) & csrcs
        }
        if len(matches) != 1:
            return None
        [moved] = matches
        uuid = self._by_slot.pop(moved)
        self._by_slot[slot] = uuid
        record = self._rows[uuid]
        record.slot = slot
        record.followed = True
        record.history.append("moved")
        self._bind()
        self._emit(
            {
                "event": "tenure_moved",
                "label": record.label,
                "from_slot": moved,
                "to_slot": slot,
                "csrcs": sorted(csrcs),
            }
        )
        return uuid

    def _contended(self) -> bool:
        return len(self._present) > self._capacity

    def _demand_full(self) -> bool:
        return len(self._by_slot) >= self._capacity

    def _release(self, device: str) -> None:
        self._pending_claims.pop(device, None)
        owned = [slot for slot, uuid in self._by_slot.items() if self._rows[uuid].device == device]
        if owned:
            for slot in owned:
                uuid = self._by_slot.pop(slot)
                record = self._rows[uuid]
                record.slot = None
                record.history.append("released")
                self._dormant[device] = (slot, uuid)
                self._emit(
                    {
                        "event": "tenure_released",
                        "label": record.label,
                        "slot": slot,
                        "device": device,
                        "dormant": True,
                    }
                )
            return
        for slot, uuid in self._by_slot.items():
            record = self._rows[uuid]
            if record.device is None:
                del self._by_slot[slot]
                record.slot = None
                record.history.append("released")
                self._emit(
                    {
                        "event": "tenure_released",
                        "label": record.label,
                        "slot": slot,
                        "device": None,
                        "dormant": False,
                    }
                )
                return

    def _unbound_devices(self) -> set[str]:
        return set(self._present) - {self._rows[uuid].device for uuid in self._by_slot.values()}

    def _unbound_rows(self) -> list[_Record]:
        return [
            self._rows[uuid] for uuid in self._by_slot.values() if self._rows[uuid].device is None
        ]

    def _bind(self) -> None:
        devices = self._unbound_devices()
        rows = self._unbound_rows()
        if len(devices) == 1 and len(rows) == 1:
            device = devices.pop()
            rows[0].device = device
            pending = self._pending_claims.get(device)
            if pending is not None:
                self._apply_claim(device, pending)
