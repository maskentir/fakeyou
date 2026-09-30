import math
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from fakeyou.faults import FaultReporter
from fakeyou.journal import ProviderJournal

DeafVerdict = Literal[
    "hearing", "silent", "unavailable", "alone", "capture_fault", "stt_fault", "capture_silent"
]

ENERGY_THRESHOLD = 0.005


def rms_level(pcm: bytes, *, channels: int = 1) -> float:
    stride = 2 * max(channels, 1)
    count = len(pcm) // stride
    if count == 0:
        return 0.0
    values = struct.unpack(f"<{count * max(channels, 1)}h", pcm[: count * stride])
    return math.sqrt(sum(v * v for v in values) / len(values)) / 32768.0


def audio_duration_ms(num_bytes: int, sample_rate: int, channels: int) -> float:
    if sample_rate <= 0 or num_bytes <= 0:
        return 0.0
    samples = num_bytes // (2 * max(channels, 1))
    return samples / sample_rate * 1000


@dataclass(frozen=True, slots=True)
class CaptureSnapshot:
    available: bool
    frames_delivered: int
    finals_delivered: int
    last_frame_at: float | None
    last_final_at: float | None
    ready_at: float | None
    interim_count: int
    last_speech_at: float | None = None
    speech_active: bool = False
    pending_speech_at: float | None = None
    remote_participants: int | None = None
    presence_at: float | None = None
    alone_since: float | None = None
    energetic_ms: float = 0.0
    last_energetic_at: float | None = None


class CaptureActivity:
    def __init__(self, *, now: Callable[[], float] | None = None) -> None:
        self._now = now or time.monotonic
        self._available = False
        self._frames_delivered = 0
        self._finals_delivered = 0
        self._last_frame_at: float | None = None
        self._last_final_at: float | None = None
        self._ready_at: float | None = None
        self._interim_count = 0
        self._last_speech_at: float | None = None
        self._speech_active = False
        self._pending_speech_at: float | None = None
        self._remote_participants: int | None = None
        self._presence_at: float | None = None
        self._alone_since: float | None = None
        self._energetic_ms = 0.0
        self._last_energetic_at: float | None = None

    def mark_ready(self) -> None:
        self._available = True
        self._ready_at = self._now()

    def observe_audio(self, *, energy: float | None = None, duration_ms: float = 0.0) -> None:
        self._frames_delivered += 1
        self._last_frame_at = self._now()
        if energy is not None and energy >= ENERGY_THRESHOLD:
            self._energetic_ms += max(duration_ms, 0.0)
            self._last_energetic_at = self._last_frame_at

    def observe_speech(self, active: bool) -> None:
        self._speech_active = active
        self._last_speech_at = self._now()
        if active and self._pending_speech_at is None:
            self._pending_speech_at = self._last_speech_at

    def observe_transcript(self, finalized: bool) -> None:
        stamp = self._now()
        if finalized:
            self._finals_delivered += 1
            self._last_final_at = stamp
            self._pending_speech_at = stamp if self._speech_active else None
        else:
            self._interim_count += 1

    def observe_frame(self, finalized: bool) -> None:
        self.observe_transcript(finalized)

    def observe_presence(self, count: int | None) -> None:
        stamp = self._now()
        if count is None:
            if self._remote_participants is not None and self._remote_participants > 0:
                return
            self._alone_since = None
            self._presence_at = None
            self._remote_participants = None
            return
        if count is not None and count < 0:
            raise ValueError("participant count cannot be negative")
        if count != 0:
            self._alone_since = None
        elif (
            self._presence_at is None or stamp - self._presence_at > 30 or self._alone_since is None
        ):
            self._alone_since = stamp
        self._presence_at = stamp
        self._remote_participants = count

    def mark_unavailable(self) -> None:
        self._available = False

    def snapshot(self) -> CaptureSnapshot:
        return CaptureSnapshot(
            self._available,
            self._frames_delivered,
            self._finals_delivered,
            self._last_frame_at,
            self._last_final_at,
            self._ready_at,
            self._interim_count,
            self._last_speech_at,
            self._speech_active,
            self._pending_speech_at,
            self._remote_participants,
            self._presence_at,
            self._alone_since,
            self._energetic_ms,
            self._last_energetic_at,
        )


def evaluate_deafness(
    snapshot: CaptureSnapshot,
    now: float,
    *,
    deaf_window_seconds: float = 120.0,
    deaf_grace_seconds: float = 120.0,
) -> DeafVerdict:
    if not snapshot.available:
        return "unavailable"
    heartbeat = snapshot.last_frame_at if snapshot.last_frame_at is not None else snapshot.ready_at
    if heartbeat is not None and now - heartbeat > 30:
        return "capture_fault"
    if (
        snapshot.ready_at is not None
        and now - snapshot.ready_at >= deaf_grace_seconds
        and snapshot.frames_delivered > 0
        and snapshot.last_energetic_at is None
        and snapshot.remote_participants is not None
        and snapshot.remote_participants > 0
        and snapshot.presence_at is not None
        and now - snapshot.presence_at <= 30
    ):
        return "capture_silent"
    if (
        snapshot.remote_participants == 0
        and snapshot.presence_at is not None
        and now - snapshot.presence_at <= 30
        and snapshot.alone_since is not None
        and now - snapshot.alone_since >= 600
    ):
        return "alone"
    if (
        snapshot.pending_speech_at is not None
        and now - snapshot.pending_speech_at >= deaf_window_seconds
    ):
        return "stt_fault"
    if snapshot.last_final_at is not None and now - snapshot.last_final_at <= 30:
        return "hearing"
    return "silent"


@dataclass(frozen=True, slots=True)
class CaptureFault:
    kind: str
    source: str = "stt"
    status: int | None = None
    detail: str | None = None


class DeafMonitor:
    def __init__(
        self,
        *,
        activity: CaptureActivity,
        reporter: FaultReporter | None = None,
        deaf_window_seconds: float = 120.0,
        deaf_grace_seconds: float = 120.0,
        restate_interval_seconds: float = 60.0,
        log: Callable[[str], None] | None = None,
        journal: ProviderJournal | None = None,
    ) -> None:
        self._activity = activity
        self._reporter = reporter
        self.deaf_window_seconds = deaf_window_seconds
        self.deaf_grace_seconds = deaf_grace_seconds
        self.restate_interval_seconds = restate_interval_seconds
        self._journal = journal
        self._first_fault_at: float | None = None
        self._last_report_at: float | None = None
        self._fault_kind: str | None = None

    def verdict(self, now: float) -> DeafVerdict:
        return evaluate_deafness(
            self._activity.snapshot(),
            now,
            deaf_window_seconds=self.deaf_window_seconds,
            deaf_grace_seconds=self.deaf_grace_seconds,
        )

    def _report(self, verdict: DeafVerdict) -> None:
        if self._reporter is not None and verdict != "capture_silent":
            self._reporter.record(CaptureFault(verdict))
        if self._journal is not None and verdict == "capture_silent":
            snapshot = self._activity.snapshot()
            self._journal.record(
                "capture_silent",
                frames_delivered=snapshot.frames_delivered,
                energetic_ms=round(snapshot.energetic_ms, 1),
                remote_participants=snapshot.remote_participants,
            )

    def note_tick(self, now: float, verdict: DeafVerdict) -> str | None:
        if verdict not in ("capture_fault", "stt_fault", "capture_silent"):
            self._first_fault_at = None
            self._last_report_at = None
            self._fault_kind = None
            return None
        first = self._first_fault_at
        if first is None or self._fault_kind != verdict:
            self._first_fault_at = now
            self._last_report_at = now
            self._fault_kind = verdict
            self._report(verdict)
            return "repair" if verdict == "stt_fault" else verdict
        if now - first >= self.deaf_grace_seconds and verdict != "capture_silent":
            return "terminal"
        if (
            self._last_report_at is not None
            and now - self._last_report_at >= self.restate_interval_seconds
        ):
            self._last_report_at = now
            self._report(verdict)
            return "restate"
        return None
