import json
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE
from fakeyou.observability.journal import Journal


@dataclass(frozen=True, slots=True)
class AudioRecordingSummary:
    slots: tuple[int, ...]
    seconds_total: float
    bytes_total: int
    gaps_filled: int
    chunks: int


@dataclass(slots=True)
class _SlotState:
    handle: wave.Wave_write
    seq_start: int
    seq_end: int
    frames: int = 0
    gaps_filled: int = 0


@dataclass(slots=True)
class _Anchor:
    slot: int
    seq: int
    page_ms: float
    mono: float


class SlotAudioWriter:
    def __init__(
        self,
        directory: Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        journal: Journal | None = None,
    ) -> None:
        self._directory = directory
        self._monotonic = monotonic
        self._journal = journal
        self._slots: dict[int, _SlotState] = {}
        self._anchors: list[_Anchor] = []
        self._chunks = 0
        self._summary: AudioRecordingSummary | None = None
        directory.mkdir(parents=True, exist_ok=True)
        if journal is not None:
            journal.record(
                "audio_recording_started", source="meeting_record", directory=directory.name
            )

    def write_chunk(self, slot: int, seq: int, page_ms: float, pcm: bytes) -> None:
        if self._summary is not None:
            raise ValueError("writer is closed")
        state = self._slots.get(slot)
        if state is None:
            handle = wave.open(str(self._directory / f"slot-{slot}.wav"), "wb")
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(CAPTURE_SAMPLE_RATE)
            state = _SlotState(handle=handle, seq_start=seq, seq_end=seq)
            self._slots[slot] = state
            expected = seq
        else:
            if seq <= state.seq_end:
                return
            expected = state.seq_end + 1
        if seq > expected:
            missed = seq - expected
            state.handle.writeframes(bytes(missed * len(pcm)))
            state.frames += missed * (len(pcm) // 2)
            state.gaps_filled += 1
        state.handle.writeframes(pcm)
        state.frames += len(pcm) // 2
        state.seq_end = seq
        self._anchors.append(_Anchor(slot=slot, seq=seq, page_ms=page_ms, mono=self._monotonic()))
        self._chunks += 1

    def close(self) -> AudioRecordingSummary:
        if self._summary is not None:
            return self._summary
        for state in self._slots.values():
            state.handle.close()
        slots: dict[str, dict[str, int]] = {
            str(slot): {
                "seq_start": state.seq_start,
                "seq_end": state.seq_end,
                "frames": state.frames,
                "gaps_filled": state.gaps_filled,
            }
            for slot, state in sorted(self._slots.items())
        }
        anchors: list[dict[str, Any]] = [
            {"slot": anchor.slot, "seq": anchor.seq, "page_ms": anchor.page_ms, "mono": anchor.mono}
            for anchor in self._anchors
        ]
        payload = {"schema": "slot_audio/v1", "slots": slots, "anchors": anchors}
        (self._directory / "slots.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        bytes_total = sum(state.frames for state in self._slots.values()) * 2
        summary = AudioRecordingSummary(
            slots=tuple(sorted(self._slots)),
            seconds_total=round(bytes_total / (CAPTURE_SAMPLE_RATE * 2), 3),
            bytes_total=bytes_total,
            gaps_filled=sum(state.gaps_filled for state in self._slots.values()),
            chunks=self._chunks,
        )
        self._summary = summary
        if self._journal is not None:
            self._journal.record(
                "audio_recording_finished",
                source="meeting_record",
                slots=list(summary.slots),
                seconds_total=summary.seconds_total,
                bytes_total=summary.bytes_total,
                gaps_filled=summary.gaps_filled,
                chunks=summary.chunks,
            )
        return summary
