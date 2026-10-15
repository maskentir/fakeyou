from fakeyou.audio.voice_activity import VoiceActivityTracker
from tests._fakes_common import FakeClock
from tests._voice_support import frames_batch, speak, tracker_at


def test_csrc_mode_labels_the_participant_on_the_dominant_slot() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(
            11_000.0,
            [[10_300.0, 0, 0.05, [42, 111]], [10_300.0, 1, 0.4, [42, 222]]],
        ),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    attribution = tracker.attribute()
    assert (attribution.participant_id, attribution.reason, attribution.slot) == (
        "speaker-a",
        "dominant_csrc",
        1,
    )
    assert attribution.journal_fields()["speaker_csrc"] == [222]


def test_csrc_label_follows_the_participant_across_slots() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(11_000.0, [[10_300.0, 0, 0.4, [42, 111]], [10_350.0, 1, 0.02, [42, 222]]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-a"
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        frames_batch(13_000.0, [[12_300.0, 2, 0.4, [42, 111]], [12_300.0, 1, 0.02, [42, 222]]]),
        sent_ms=13_000.0,
        received_ms=13_000.0,
    )
    moved = tracker.attribute()
    assert (moved.participant_id, moved.slot) == ("speaker-a", 2)


def test_csrc_label_survives_the_marker_being_learned_later() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(11_000.0, [[10_300.0, 0, 0.4, [42, 111]]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-a"
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        frames_batch(13_000.0, [[12_300.0, 1, 0.4, [42, 222]]]),
        sent_ms=13_000.0,
        received_ms=13_000.0,
    )
    second = tracker.attribute()
    assert (second.participant_id, second.journal_fields()["speaker_csrc"]) == (
        "speaker-b",
        [222],
    )
    speak(tracker, clock, 14.0, 15.0)
    tracker.observe_batch(
        frames_batch(15_000.0, [[14_300.0, 0, 0.4, [42, 111]]]),
        sent_ms=15_000.0,
        received_ms=15_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-a"


def test_csrc_mode_reports_overlap_and_conflict_as_unknown() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(11_000.0, [[10_300.0, 0, 0.3, [42, 111]], [10_300.0, 1, 0.25, [42, 222]]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    assert tracker.attribute().reason == "overlap"
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        frames_batch(13_000.0, [[12_200.0, 0, 0.4, [42, 111]], [12_250.0, 1, 0.01, [42, 222]]]),
        sent_ms=13_000.0,
        received_ms=13_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-a"
    speak(tracker, clock, 14.0, 15.0)
    tracker.observe_batch(
        frames_batch(15_000.0, [[14_200.0, 1, 0.4, [42, 222]]]),
        sent_ms=15_000.0,
        received_ms=15_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-b"
    speak(tracker, clock, 16.0, 17.0)
    tracker.observe_batch(
        frames_batch(17_000.0, [[16_200.0, 0, 0.4, [42, 111]], [16_500.0, 0, 0.4, [42, 222]]]),
        sent_ms=17_000.0,
        received_ms=17_000.0,
    )
    assert tracker.attribute().reason == "csrc_conflict"


def test_csrc_mode_ignores_webaudio_taps_and_twin_playback() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    tracker.observe_self_speech(active=True)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_self_speech(active=False)
    tracker.observe_batch(
        {
            **frames_batch(11_000.0, [[10_300.0, 0, 0.3, [42, 111]]]),
            "levels": [[10_300.0, 5, 0.9]],
        },
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    attribution = tracker.attribute()
    assert (attribution.participant_id, attribution.slot) == ("speaker-a", 0)


def test_csrc_mode_shifts_frames_by_their_own_lag() -> None:
    clock = FakeClock(now=10.0)
    tracker = VoiceActivityTracker(twin_name="Twin", monotonic=clock.monotonic, frame_lag_ms=500.0)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(11_000.0, [[9_600.0, 0, 0.3, [111]], [10_800.0, 1, 0.3, [222]]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    assert tracker.attribute().slot == 0


def test_csrc_mode_keeps_labels_across_page_reload() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(11_000.0, [[10_300.0, 0, 0.3, [111]]]), sent_ms=11_000.0, received_ms=11_000.0
    )
    assert tracker.attribute().participant_id == "speaker-a"
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        {**frames_batch(500.0, [[-200.0, 0, 0.3, [111]]]), "instance": "i2"},
        sent_ms=13_000.0,
        received_ms=13_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-a"


def test_csrc_attribution_carries_speaker_uuid() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(11_000.0, [[10_300.0, 0, 0.4, [42, 111]]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    attribution = tracker.attribute()
    assert attribution.speaker_uuid
    assert attribution.journal_fields()["speaker_uuid"] == attribution.speaker_uuid
