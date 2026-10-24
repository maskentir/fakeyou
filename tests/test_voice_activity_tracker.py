from fakeyou.audio.voice_activity import VoiceActivityTracker
from tests._fakes_common import FakeClock
from tests._voice_support import batch, speak, tracker_at


def test_tracker_labels_dominant_slot_with_pipeline_lag() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.2, 11.2)
    clock.now = 11.4
    tracker.observe_batch(
        batch(1000.0 + 11_400.0, [[1000.0 + 10_050.0, 3, 0.3], [1000.0 + 10_100.0, 4, 0.05]]),
        sent_ms=11_400.0,
        received_ms=11_400.0,
    )
    attribution = tracker.attribute()
    assert attribution.participant_id == "speaker-a"
    assert attribution.reason == "dominant_slot"
    assert attribution.slot == 3
    assert attribution.bound is True
    assert attribution.evidence_id == "voice-1"
    assert attribution.journal_fields()["speaker_levels"] == {"3": 0.3, "4": 0.05}


def test_tracker_reports_overlap_as_unknown() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock, ("d1", "d2"))
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [[10_300.0, 3, 0.2], [10_400.0, 4, 0.15]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    attribution = tracker.attribute()
    assert (attribution.participant_id, attribution.reason) == ("unknown", "overlap")


def test_tracker_needs_speech_fresh_taps_and_a_loud_source() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    assert tracker.attribute().reason == "no_speech"
    speak(tracker, clock, 10.0, 11.0)
    assert tracker.attribute().reason == "stale"
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        batch(13_000.0, [[12_500.0, 3, 0.004]]), sent_ms=13_000.0, received_ms=13_000.0
    )
    assert tracker.attribute().reason == "no_source"


def test_tracker_calls_partly_drained_silence_stale() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(10_300.0, [[10_000.0, 3, 0.004]]), sent_ms=10_300.0, received_ms=10_300.0
    )
    assert tracker.attribute().reason == "stale"


def test_tracker_only_counts_speech_since_previous_transcript() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock, ("d1", "d2"))
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [[10_400.0, 3, 0.3]]), sent_ms=11_000.0, received_ms=11_000.0
    )
    assert tracker.attribute().slot == 3
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        batch(13_000.0, [[12_400.0, 4, 0.3]]), sent_ms=13_000.0, received_ms=13_000.0
    )
    second = tracker.attribute()
    assert (second.slot, second.participant_id) == (4, "speaker-b")


def test_tracker_splits_open_speech_between_transcripts() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    tracker.observe_vad(active=True)
    clock.now = 12.0
    tracker.observe_batch(
        batch(12_000.0, [[10_500.0, 3, 0.3]]), sent_ms=12_000.0, received_ms=12_000.0
    )
    assert tracker.attribute().slot == 3
    clock.now = 14.0
    tracker.observe_batch(batch(14_000.0, []), sent_ms=14_000.0, received_ms=14_000.0)
    assert tracker.attribute().reason == "no_source"


def test_tracker_maps_page_clock_with_lowest_round_trip() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(batch(3_100.0, []), sent_ms=11_090.0, received_ms=11_110.0)
    tracker.observe_batch(
        batch(6_000.0, [[2_300.0, 3, 0.3]]), sent_ms=12_000.0, received_ms=14_000.0
    )
    assert tracker.attribute().slot == 3


def test_tracker_ignores_local_slots() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [[10_300.0, 0, 0.5], [10_300.0, 3, 0.2]], local=[0]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    attribution = tracker.attribute()
    assert (attribution.slot, attribution.participant_id, attribution.bound) == (
        3,
        "speaker-a",
        True,
    )


def test_tracker_refuses_new_label_while_twin_plays() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    tracker.observe_self_speech(active=True)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_self_speech(active=False)
    tracker.observe_batch(
        batch(11_000.0, [[10_300.0, 3, 0.3]]), sent_ms=11_000.0, received_ms=11_000.0
    )
    assert tracker.attribute().reason == "self_overlap"


def test_tracker_restarts_labels_when_page_script_reloads() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [[10_300.0, 3, 0.3]]), sent_ms=11_000.0, received_ms=11_000.0
    )
    assert tracker.attribute().participant_id == "speaker-a"
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        {**batch(500.0, [[-200.0, 3, 0.3]]), "instance": "i2"},
        sent_ms=13_000.0,
        received_ms=13_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-b"


def test_tracker_skips_malformed_levels() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [["x", 3, 0.9], [10_300.0, None, 0.9], [10_300.0], [10_300.0, 3, 0.2]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    tracker.observe_batch({"page_ms": "late"}, sent_ms=11_000.0, received_ms=11_000.0)
    assert tracker.attribute().slot == 3


def test_tracker_binds_two_humans_with_unique_names() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock, ("devices/1", "devices/2"))
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [[10_400.0, 3, 0.3]]), sent_ms=11_000.0, received_ms=11_000.0
    )
    first = tracker.attribute()
    assert (first.participant_id, first.slot) == ("speaker-a", 3)
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        batch(13_000.0, [[12_400.0, 4, 0.3]]), sent_ms=13_000.0, received_ms=13_000.0
    )
    second = tracker.attribute()
    assert (second.participant_id, second.slot) == ("speaker-b", 4)


def test_dominant_slot_attribution_carries_speaker_uuid() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.2, 11.2)
    clock.now = 11.4
    tracker.observe_batch(
        batch(1000.0 + 11_400.0, [[1000.0 + 10_050.0, 3, 0.3], [1000.0 + 10_100.0, 4, 0.05]]),
        sent_ms=11_400.0,
        received_ms=11_400.0,
    )
    attribution = tracker.attribute()
    assert attribution.reason == "dominant_slot"
    assert attribution.speaker_uuid
    assert attribution.journal_fields()["speaker_uuid"] == attribution.speaker_uuid


def test_others_speaking_is_true_while_a_remote_voice_is_active() -> None:
    clock = FakeClock(now=10.0)
    tracker = VoiceActivityTracker(monotonic=clock.monotonic)
    clock.now = 10.0
    tracker.observe_vad(active=True)
    clock.now = 10.2

    assert tracker.others_speaking() is True


def test_others_speaking_clears_after_the_hangover() -> None:
    clock = FakeClock(now=10.0)
    tracker = VoiceActivityTracker(monotonic=clock.monotonic)
    speak(tracker, clock, 10.0, 10.5)
    clock.now = 11.1

    assert tracker.others_speaking() is False


def test_own_playback_does_not_count_as_others_speaking() -> None:
    clock = FakeClock(now=10.0)
    tracker = VoiceActivityTracker(monotonic=clock.monotonic)
    clock.now = 10.0
    tracker.observe_self_speech(active=True)
    speak(tracker, clock, 10.0, 10.8)
    clock.now = 10.8

    assert tracker.others_speaking() is False


def test_overlap_is_unbound_not_loudest_slot() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [[10_300.0, 3, 0.3], [10_400.0, 4, 0.2]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    attribution = tracker.attribute()
    assert attribution.bound is False
    assert attribution.reason == "overlap"


def test_twin_own_speech_gets_no_participant_label() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    tracker.observe_self_speech(active=True)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_self_speech(active=False)
    tracker.observe_batch(
        batch(11_000.0, [[10_300.0, 3, 0.3]]), sent_ms=11_000.0, received_ms=11_000.0
    )
    attribution = tracker.attribute()
    assert attribution.bound is False
    assert not attribution.participant_id.startswith("speaker-")


def test_anonymous_label_is_stable_and_never_a_display_name() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [[10_300.0, 3, 0.3]]), sent_ms=11_000.0, received_ms=11_000.0
    )
    first = tracker.attribute()
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        batch(13_000.0, [[12_300.0, 3, 0.3]]), sent_ms=13_000.0, received_ms=13_000.0
    )
    second = tracker.attribute()
    assert first.participant_id == second.participant_id == "speaker-a"
    assert "Budi" not in first.participant_id
