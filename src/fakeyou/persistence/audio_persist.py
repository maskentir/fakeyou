import json
import os
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES
from fakeyou.observability.journal import Journal
from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.layout import ANCHORS_FILE_NAME, SLOTS_FILE_NAME

SLOT_AUDIO_SCHEMA = "slot_audio/v2"
MAX_SLOTS = 16
MAX_GAP_FILL_SECONDS = 2.0
REORDER_WINDOW_CHUNKS = 50
HEADER_PATCH_INTERVAL = 50
METADATA_INTERVAL = 50
ANCHOR_SYNC_INTERVAL = 50
_MAX_GAP_FILL_BYTES = int(MAX_GAP_FILL_SECONDS * CAPTURE_SAMPLE_RATE) * PCM16_SAMPLE_BYTES
_WAV_HEADER = struct.Struct("<4sI4s4sIHHIIHH4sI")


@dataclass(frozen=True, slots=True)
class AudioRecordingSummary:
    slots: tuple[int, ...]
    seconds_total: float
    bytes_total: int
    gaps_filled: int
    chunks: int


@dataclass(slots=True)
class _SlotState:
    handle: BinaryIO
    seq_start: int
    seq_end: int
    frames: int = 0
    gaps_filled: int = 0
    gaps_truncated: int = 0
    restarts: int = 0
    chunks_since_patch: int = 0


def _wav_header(data_bytes: int) -> bytes:
    return _WAV_HEADER.pack(
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        CAPTURE_SAMPLE_RATE,
        CAPTURE_SAMPLE_RATE * PCM16_SAMPLE_BYTES,
        PCM16_SAMPLE_BYTES,
        8 * PCM16_SAMPLE_BYTES,
        b"data",
        data_bytes,
    )


def read_slot_anchors(directory: Path) -> list[dict[str, Any]]:
    try:
        text = (directory / ANCHORS_FILE_NAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    anchors: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
        except ValueError:
            if index == len(lines) - 1 and not text.endswith("\n"):
                break
            raise ValueError(f"anchor line {index + 1} is corrupt") from None
        if not isinstance(row, dict):
            raise TypeError(f"anchor line {index + 1} is not an object")
        anchors.append(row)
    return anchors


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
        self._chunks = 0
        self._unsynced_anchors = 0
        self._summary: AudioRecordingSummary | None = None
        directory.mkdir(parents=True, exist_ok=True)
        self._anchors: BinaryIO = (directory / ANCHORS_FILE_NAME).open("ab")
        if journal is not None:
            journal.record(
                "audio_recording_started", source="meeting_record", directory=directory.name
            )

    def write_chunk(self, slot: int, seq: int, page_ms: float, pcm: bytes) -> None:
        if self._summary is not None:
            raise ValueError("writer is closed")
        if not 0 <= slot < MAX_SLOTS:
            raise ValueError(f"slot {slot} is outside 0..{MAX_SLOTS - 1}")
        state = self._slots.get(slot)
        if state is None:
            state = self._open_slot(slot, seq)
        if seq <= state.seq_end:
            if state.seq_end - seq < REORDER_WINDOW_CHUNKS:
                return
            state.restarts += 1
            expected = seq
        else:
            expected = state.seq_end + 1
        if seq > expected:
            self._fill_gap(state, seq - expected, len(pcm))
        state.handle.write(pcm)
        state.frames += len(pcm) // PCM16_SAMPLE_BYTES
        state.seq_end = seq
        state.chunks_since_patch += 1
        if state.chunks_since_patch >= HEADER_PATCH_INTERVAL:
            self._patch_header(state)
        self._append_anchor(slot, seq, page_ms)
        self._chunks += 1
        if self._chunks % METADATA_INTERVAL == 0:
            self._write_metadata()

    def close(self) -> AudioRecordingSummary:
        if self._summary is not None:
            return self._summary
        for state in self._slots.values():
            self._patch_header(state)
            state.handle.close()
        self._anchors.flush()
        os.fsync(self._anchors.fileno())
        self._anchors.close()
        self._write_metadata()
        bytes_total = sum(state.frames for state in self._slots.values()) * PCM16_SAMPLE_BYTES
        summary = AudioRecordingSummary(
            slots=tuple(sorted(self._slots)),
            seconds_total=round(bytes_total / (CAPTURE_SAMPLE_RATE * PCM16_SAMPLE_BYTES), 3),
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

    def _open_slot(self, slot: int, seq: int) -> _SlotState:
        handle: BinaryIO = (self._directory / f"slot-{slot}.wav").open("wb")
        handle.write(_wav_header(0))
        state = _SlotState(handle=handle, seq_start=seq, seq_end=seq - 1)
        self._slots[slot] = state
        return state

    def _fill_gap(self, state: _SlotState, missed_chunks: int, chunk_bytes: int) -> None:
        wanted = missed_chunks * chunk_bytes
        fill = min(wanted, _MAX_GAP_FILL_BYTES)
        state.handle.write(bytes(fill))
        state.frames += fill // PCM16_SAMPLE_BYTES
        state.gaps_filled += 1
        if fill < wanted:
            state.gaps_truncated += 1

    def _patch_header(self, state: _SlotState) -> None:
        state.handle.flush()
        state.handle.seek(0)
        state.handle.write(_wav_header(state.frames * PCM16_SAMPLE_BYTES))
        state.handle.seek(0, os.SEEK_END)
        state.handle.flush()
        state.chunks_since_patch = 0

    def _append_anchor(self, slot: int, seq: int, page_ms: float) -> None:
        row = {"slot": slot, "seq": seq, "page_ms": page_ms, "mono": self._monotonic()}
        self._anchors.write((json.dumps(row) + "\n").encode("utf-8"))
        self._anchors.flush()
        self._unsynced_anchors += 1
        if self._unsynced_anchors >= ANCHOR_SYNC_INTERVAL:
            os.fsync(self._anchors.fileno())
            self._unsynced_anchors = 0

    def _write_metadata(self) -> None:
        slots: dict[str, dict[str, int]] = {
            str(slot): {
                "seq_start": state.seq_start,
                "seq_end": state.seq_end,
                "frames": state.frames,
                "gaps_filled": state.gaps_filled,
                "gaps_truncated": state.gaps_truncated,
                "restarts": state.restarts,
            }
            for slot, state in sorted(self._slots.items())
        }
        payload = {"schema": SLOT_AUDIO_SCHEMA, "slots": slots}
        write_atomically(
            self._directory / SLOTS_FILE_NAME, json.dumps(payload, indent=2).encode("utf-8")
        )
