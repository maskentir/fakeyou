from __future__ import annotations

import bisect
import signal
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from evals.writer_spike.pulse import SAMPLE_BYTES, Client, Recorder

MONITOR = "agent_output.monitor"
FRAGMENT_BYTES = 240
HEADER = struct.Struct("<qI")
MIN_ATTRIBUTABLE_BYTES = 16


@dataclass(frozen=True, slots=True)
class Run:
    offset: int
    data: bytes
    first_received_at: int
    last_received_at: int


@dataclass(frozen=True, slots=True)
class Recording:
    audio: bytes
    starts: tuple[int, ...]
    received: tuple[int, ...]

    def received_at(self, offset: int) -> int:
        return self.received[bisect.bisect_right(self.starts, offset) - 1]

    def runs(self) -> list[Run]:
        found: list[Run] = []
        samples = memoryview(self.audio).cast("h")
        start: int | None = None
        for position, sample in enumerate(samples):
            if sample != 0 and start is None:
                start = position
            elif sample == 0 and start is not None:
                found.append(self._run(start, position))
                start = None
        if start is not None:
            found.append(self._run(start, len(samples)))
        return found

    def _run(self, start: int, stop: int) -> Run:
        first, last = start * SAMPLE_BYTES, stop * SAMPLE_BYTES
        return Run(
            first,
            self.audio[first:last],
            self.received_at(first),
            self.received_at(last - SAMPLE_BYTES),
        )


def load(path: Path) -> Recording:
    raw = path.read_bytes()
    audio = bytearray()
    starts: list[int] = []
    received: list[int] = []
    position = 0
    while position + HEADER.size <= len(raw):
        at, size = HEADER.unpack_from(raw, position)
        position += HEADER.size
        if position + size > len(raw):
            break
        starts.append(len(audio))
        received.append(at)
        audio += raw[position : position + size]
        position += size
    return Recording(bytes(audio), tuple(starts), tuple(received))


def record(output: BinaryIO) -> None:
    stopping = False

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def sink(received_at: int, chunk: bytes) -> None:
        output.write(HEADER.pack(received_at, len(chunk)))
        output.write(chunk)

    client = Client("writer-spike-observer")
    Recorder(client, MONITOR, FRAGMENT_BYTES, sink)
    sys.stdout.write("ready\n")
    sys.stdout.flush()
    while not stopping:
        client.pump(0.05)
        output.flush()
    output.flush()


if __name__ == "__main__":
    with Path(sys.argv[1]).open("wb") as target:
        record(target)
