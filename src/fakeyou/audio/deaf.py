import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from fakeyou.config.settings import DEFAULTS
from fakeyou.foundation.invariants import PCM_CHANNELS
from fakeyou.foundation.numbers import rms
from fakeyou.foundation.pcm import PCM16_FULL_SCALE, PCM16_SAMPLE_BYTES, decode_pcm16
from fakeyou.foundation.taxonomy import Subsystem
from fakeyou.observability.faults import Fault, FaultReporter
from fakeyou.observability.journal import Journal

DeafVerdict = Literal[
    "hearing",
    "silent",
    "unavailable",
    "alone",
    "no_audio_frames",
    "no_transcripts",
    "silent_capture",
]

ENERGY_THRESHOLD = 0.005
_VERDICT_SOURCES: dict[DeafVerdict, Subsystem] = {
    "no_audio_frames": "audio",
    "no_transcripts": "stt",
}


def rms_level(pcm: bytes, *, channels: int = PCM_CHANNELS) -> float:
    stride = PCM16_SAMPLE_BYTES * max(channels, 1)
    count = len(pcm) // stride
    if count == 0:
        return 0.0
    values = decode_pcm16(pcm[: count * stride])
    return rms(values) / PCM16_FULL_SCALE


def audio_duration_ms(num_bytes: int, sample_rate: int, channels: int) -> float:
    if sample_rate <= 0 or num_bytes <= 0:
        return 0.0
    samples = num_bytes // (PCM16_SAMPLE_BYTES * max(channels, 1))
    return samples / sample_rate * 1000


@dataclass(slots=True)
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
        self._snapshot = CaptureSnapshot(
            available=False,
            frames_delivered=0,
            finals_delivered=0,
            last_frame_at=None,
            last_final_at=None,
            ready_at=None,
            interim_count=0,
        )

    def mark_ready(self) -> None:
        self._snapshot.available = True
        self._snapshot.ready_at = self._now()

    def observe_audio(self, *, energy: float | None = None, duration_ms: float = 0.0) -> None:
        self._snapshot.frames_delivered += 1
        self._snapshot.last_frame_at = self._now()
        if energy is not None and energy >= ENERGY_THRESHOLD:
            self._snapshot.energetic_ms += max(duration_ms, 0.0)
            self._snapshot.last_energetic_at = self._snapshot.last_frame_at

    def observe_speech(self, *, active: bool) -> None:
        self._snapshot.speech_active = active
        self._snapshot.last_speech_at = self._now()
        if active and self._snapshot.pending_speech_at is None:
            self._snapshot.pending_speech_at = self._snapshot.last_speech_at

    def observe_transcript(self, *, finalized: bool) -> None:
        stamp = self._now()
        if finalized:
            self._snapshot.finals_delivered += 1
            self._snapshot.last_final_at = stamp
            self._snapshot.pending_speech_at = stamp if self._snapshot.speech_active else None
        else:
            self._snapshot.interim_count += 1

    def observe_presence(self, count: int | None) -> None:
        stamp = self._now()
        if count is None:
            if (
                self._snapshot.remote_participants is not None
                and self._snapshot.remote_participants > 0
            ):
                return
            self._snapshot.alone_since = None
            self._snapshot.presence_at = None
            self._snapshot.remote_participants = None
            return
        if count is not None and count < 0:
            raise ValueError("participant count cannot be negative")
        if count != 0:
            self._snapshot.alone_since = None
        elif (
            self._snapshot.presence_at is None
            or stamp - self._snapshot.presence_at > 30
            or self._snapshot.alone_since is None
        ):
            self._snapshot.alone_since = stamp
        self._snapshot.presence_at = stamp
        self._snapshot.remote_participants = count

    def mark_unavailable(self) -> None:
        self._snapshot.available = False

    def snapshot(self) -> CaptureSnapshot:
        return replace(self._snapshot)


def evaluate_deafness(
    snapshot: CaptureSnapshot,
    now: float,
    *,
    deaf_window_seconds: float = DEFAULTS.audio_health.deaf_window_seconds,
    deaf_grace_seconds: float = DEFAULTS.audio_health.deaf_grace_seconds,
    alone_leave_seconds: float = DEFAULTS.audio_health.alone_leave_seconds,
) -> DeafVerdict:
    if not snapshot.available:
        return "unavailable"
    heartbeat = snapshot.last_frame_at if snapshot.last_frame_at is not None else snapshot.ready_at
    if heartbeat is not None and now - heartbeat > 30:
        return "no_audio_frames"
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
        return "silent_capture"
    if (
        snapshot.remote_participants == 0
        and snapshot.presence_at is not None
        and now - snapshot.presence_at <= 30
        and snapshot.alone_since is not None
        and now - snapshot.alone_since >= alone_leave_seconds
    ):
        return "alone"
    if (
        snapshot.pending_speech_at is not None
        and now - snapshot.pending_speech_at >= deaf_window_seconds
    ):
        return "no_transcripts"
    if snapshot.last_final_at is not None and now - snapshot.last_final_at <= 30:
        return "hearing"
    return "silent"


@dataclass(frozen=True, slots=True)
class DeafTiming:
    deaf_window_seconds: float = DEFAULTS.audio_health.deaf_window_seconds
    deaf_grace_seconds: float = DEFAULTS.audio_health.deaf_grace_seconds
    alone_leave_seconds: float = DEFAULTS.audio_health.alone_leave_seconds
    restate_interval_seconds: float = 60.0


DEFAULT_DEAF_TIMING = DeafTiming()


class DeafMonitor:
    def __init__(
        self,
        *,
        activity: CaptureActivity,
        faults: FaultReporter | None = None,
        timing: DeafTiming = DEFAULT_DEAF_TIMING,
        journal: Journal | None = None,
    ) -> None:
        self._activity = activity
        self._faults = faults
        self.alone_leave_seconds = timing.alone_leave_seconds
        self.deaf_window_seconds = timing.deaf_window_seconds
        self.deaf_grace_seconds = timing.deaf_grace_seconds
        self.restate_interval_seconds = timing.restate_interval_seconds
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
            alone_leave_seconds=self.alone_leave_seconds,
        )

    def _report(self, verdict: DeafVerdict) -> None:
        if self._faults is not None and verdict in ("no_audio_frames", "no_transcripts"):
            self._faults.record(Fault(source=_VERDICT_SOURCES[verdict], kind=verdict))
        if self._journal is not None and verdict == "silent_capture":
            snapshot = self._activity.snapshot()
            self._journal.record(
                "capture_silence_detected",
                source="audio",
                frames_delivered=snapshot.frames_delivered,
                energetic_ms=round(snapshot.energetic_ms, 1),
                remote_participants=snapshot.remote_participants,
            )

    def note_tick(self, now: float, verdict: DeafVerdict) -> str | None:
        if verdict not in ("no_audio_frames", "no_transcripts", "silent_capture"):
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
            return "repair" if verdict == "no_transcripts" else verdict
        if now - first >= self.deaf_grace_seconds and verdict != "silent_capture":
            return "terminal"
        if (
            self._last_report_at is not None
            and now - self._last_report_at >= self.restate_interval_seconds
        ):
            self._last_report_at = now
            self._report(verdict)
            return "restate"
        return None
