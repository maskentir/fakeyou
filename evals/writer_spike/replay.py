from __future__ import annotations

import array
import bisect
import hashlib
import json
import statistics
import struct
import sys
from pathlib import Path

HEADER = struct.Struct("<qI")
FRAME_BYTES = 960
CLIPS_PER_CELL = 50
STALL_GAP_NS = 250_000_000
CELLS = (
    ("at_read_index", "pumped_at_once"),
    ("at_read_index", "pump_held"),
    ("at_read_index", "daemon_stopped_before_release"),
    ("absolute_index", "pumped_at_once"),
    ("absolute_index", "pump_held"),
    ("absolute_index", "daemon_stopped_before_release"),
)


def clip(clip_id: int) -> bytes:
    samples = array.array("h")
    samples.frombytes(hashlib.shake_256(clip_id.to_bytes(8, "little")).digest(FRAME_BYTES))
    for position, sample in enumerate(samples):
        if sample == 0:
            samples[position] = 1
    return samples.tobytes()


def load(path: Path) -> tuple[bytes, list[int], list[int]]:
    raw = path.read_bytes()
    audio = bytearray()
    starts: list[int] = []
    received: list[int] = []
    position = 0
    while position + HEADER.size <= len(raw):
        at, size = HEADER.unpack_from(raw, position)
        position += HEADER.size
        starts.append(len(audio))
        received.append(at)
        audio += raw[position : position + size]
        position += size
    return bytes(audio), starts, received


def cell(directory: Path, mode: str, scenario: str, base: int) -> dict[str, object]:
    audio, starts, received = load(directory / f"diag-{mode}-{scenario}.rec")
    gaps = [later - earlier for earlier, later in zip(received, received[1:], strict=False)]
    silence_before_clip: list[int] = []
    for clip_id in range(base, base + CLIPS_PER_CELL):
        position = audio.find(clip(clip_id))
        if position < 0:
            continue
        fragment = bisect.bisect_right(starts, position) - 1
        preceding = [gap for gap in gaps[:fragment] if gap > STALL_GAP_NS]
        silence_before_clip.append(preceding[-1] if preceding else 0)
    return {
        "clips_found_whole": len(silence_before_clip),
        "observer_delivery_gaps_over_250ms": sum(gap > STALL_GAP_NS for gap in gaps),
        "median_fragment_interval_ns": statistics.median(gaps),
        "clips_found_with_an_earlier_delivery_gap_over_250ms": sum(
            gap > STALL_GAP_NS for gap in silence_before_clip
        ),
    }


def main() -> None:
    directory = Path(sys.argv[1])
    outcome = {
        f"{mode}/{scenario}": cell(directory, mode, scenario, index * CLIPS_PER_CELL)
        for index, (mode, scenario) in enumerate(CELLS)
    }
    sys.stdout.write(json.dumps(outcome, indent=2) + "\n")


if __name__ == "__main__":
    main()
