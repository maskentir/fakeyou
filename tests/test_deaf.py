import math
import struct

import pytest

from fakeyou.deaf import (
    CaptureActivity,
    DeafMonitor,
    audio_duration_ms,
    evaluate_deafness,
    rms_level,
)
from fakeyou.faults import FaultReporter


@pytest.fixture
def activity():
    clock = [1000.0]
    capture = CaptureActivity(now=lambda: clock[0])
    capture.mark_ready()
    return capture, clock


def test_silent_audio_is_live_capture_without_proving_speech(activity):
    capture, clock = activity
    clock[0] += 1000
    capture.observe_audio()
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "silent"
    assert capture.snapshot().frames_delivered == 1


def test_transcripts_do_not_fake_capture_heartbeat(activity):
    capture, clock = activity
    clock[0] += 31
    capture.observe_transcript(False)
    capture.observe_transcript(True)
    assert capture.snapshot().frames_delivered == 0
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "capture_fault"


def test_stopped_capture_is_fault_even_without_speech(activity):
    capture, clock = activity
    capture.observe_audio()
    clock[0] += 31
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "capture_fault"


def test_speech_without_final_becomes_stt_fault(activity):
    capture, clock = activity
    capture.observe_speech(True)
    clock[0] += 1
    capture.observe_speech(False)
    clock[0] += 120
    capture.observe_audio()
    capture.observe_transcript(False)
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "stt_fault"
    capture.observe_transcript(True)
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "hearing"


def test_unknown_presence_never_implies_alone(activity):
    capture, clock = activity
    clock[0] += 10000
    capture.observe_audio()
    capture.observe_presence(None)
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "silent"


def test_continuous_explicit_zero_presence_required_for_alone(activity):
    capture, clock = activity
    for offset in range(0, 601, 10):
        clock[0] = 1000 + offset
        capture.observe_presence(0)
        capture.observe_audio()
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "alone"
    capture.observe_presence(None)
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "silent"


def test_stale_presence_does_not_imply_alone(activity):
    capture, clock = activity
    capture.observe_presence(0)
    clock[0] += 600
    capture.observe_audio()
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "silent"
    capture.observe_presence(0)
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "silent"


def test_unknown_presence_keeps_recent_remote_presence_sticky(activity):
    capture, clock = activity
    capture.observe_presence(2)
    clock[0] += 10
    capture.observe_presence(None)
    capture.observe_audio(energy=0.0, duration_ms=20.0)
    snapshot = capture.snapshot()
    assert snapshot.remote_participants == 2
    assert snapshot.presence_at == 1000.0
    assert evaluate_deafness(snapshot, clock[0], deaf_grace_seconds=0) == "capture_silent"


def test_capture_failure_grace_resets_after_recovery(activity):
    capture, _ = activity
    monitor = DeafMonitor(activity=capture)
    assert monitor.note_tick(1000, "capture_fault") == "capture_fault"
    assert monitor.note_tick(1119, "capture_fault") == "restate"
    assert monitor.note_tick(1120, "silent") is None
    assert monitor.note_tick(1130, "capture_fault") == "capture_fault"
    assert monitor.note_tick(1250, "capture_fault") == "terminal"


def test_fault_reports_repeat_only_every_sixty_seconds(journal, activity):
    capture, _ = activity
    reporter = FaultReporter(journal=journal)
    monitor = DeafMonitor(activity=capture, reporter=reporter, deaf_grace_seconds=600)
    assert monitor.note_tick(1000, "stt_fault") == "repair"
    assert monitor.note_tick(1059, "stt_fault") is None
    assert monitor.note_tick(1060, "stt_fault") == "restate"
    assert reporter.total() == 2


def test_unavailable_is_explicit(activity):
    capture, clock = activity
    capture.mark_unavailable()
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "unavailable"


def test_rms_level_meters_s16le_energy() -> None:
    assert rms_level(b"") == 0.0
    assert rms_level(b"\x00" * 640) == 0.0
    assert rms_level(struct.pack("<2h", 32767, -32768)) == pytest.approx(1.0, abs=1e-4)
    tone = struct.pack("<160h", *[int(16384 * math.sin(i / 10)) for i in range(160)])
    assert rms_level(tone) == pytest.approx(0.3535, abs=0.01)


def test_audio_duration_ms_times_s16le_frames() -> None:
    assert audio_duration_ms(640, 16000, 1) == 20.0
    assert audio_duration_ms(640, 16000, 2) == 10.0
    assert audio_duration_ms(640, 0, 1) == 0.0
    assert audio_duration_ms(0, 16000, 1) == 0.0


def test_observe_audio_separates_energetic_from_silent_frames(activity):
    capture, clock = activity
    capture.observe_audio(energy=0.0, duration_ms=20.0)
    capture.observe_audio(energy=0.05, duration_ms=20.0)
    snapshot = capture.snapshot()
    assert snapshot.frames_delivered == 2
    assert snapshot.energetic_ms == 20.0
    assert snapshot.last_energetic_at == clock[0]


def test_silence_with_witnesses_past_grace_is_capture_silent(activity):
    capture, clock = activity
    clock[0] += 121
    capture.observe_audio(energy=0.0, duration_ms=20.0)
    capture.observe_presence(2)
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "capture_silent"


def test_capture_silent_requires_grace_frames_presence_and_no_energy(activity):
    capture, clock = activity
    clock[0] += 121
    capture.observe_audio(energy=0.0, duration_ms=20.0)
    capture.observe_presence(2)
    assert evaluate_deafness(capture.snapshot(), clock[0], deaf_grace_seconds=600) == "silent"
    capture.observe_audio(energy=0.05, duration_ms=20.0)
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "silent"


def test_capture_silent_ignores_stale_or_empty_presence(activity):
    capture, clock = activity
    clock[0] += 121
    capture.observe_audio(energy=0.0, duration_ms=20.0)
    capture.observe_presence(None)
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "silent"
    capture.observe_presence(0)
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "silent"
    capture.observe_presence(2)
    clock[0] += 31
    capture.observe_audio(energy=0.0, duration_ms=20.0)
    assert evaluate_deafness(capture.snapshot(), clock[0]) == "silent"


def test_capture_silent_records_once_restates_and_never_terminals(journal, activity):
    capture, clock = activity
    lines = journal.lines
    reporter = FaultReporter(journal=journal)
    monitor = DeafMonitor(activity=capture, reporter=reporter, journal=journal)
    assert monitor.note_tick(1000, "capture_silent") == "capture_silent"
    assert reporter.total() == 0
    assert len(lines) == 1
    assert monitor.note_tick(1059, "capture_silent") is None
    assert monitor.note_tick(1060, "capture_silent") == "restate"
    assert reporter.total() == 0
    assert len(lines) == 2
    assert monitor.note_tick(2000, "capture_silent") == "restate"
    assert monitor.note_tick(2001, "silent") is None
    assert monitor.note_tick(2002, "capture_silent") == "capture_silent"


def test_missing_capture_heartbeat_is_reported_as_fault(journal, activity):
    capture, clock = activity
    reporter = FaultReporter(journal=journal)
    monitor = DeafMonitor(activity=capture, reporter=reporter, deaf_grace_seconds=120)
    capture.observe_presence(2)
    clock[0] += 31
    assert monitor.verdict(clock[0]) == "capture_fault"
    assert monitor.note_tick(clock[0], "capture_fault") == "capture_fault"
    assert reporter.total() == 1
