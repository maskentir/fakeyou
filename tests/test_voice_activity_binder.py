from fakeyou.audio.voice_activity import MEET_AUDIO_SLOTS, SlotBinder, Tenure, VoiceActivityTracker
from tests._fakes_common import FakeClock
from tests._voice_support import frames_batch, speak


def test_binder_binds_lone_device_and_releases_only_its_slot() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    host = binder.resolve(3)
    assert not isinstance(host, str)
    assert (host.label, host.device) == ("A", "host")
    binder.update_roster(["host", "guest"])
    guest = binder.resolve(4)
    assert not isinstance(guest, str)
    assert (guest.label, guest.device) == ("B", "guest")
    binder.update_roster(["host"])
    still_host = binder.resolve(3)
    assert not isinstance(still_host, str)
    assert still_host.label == "A"
    binder.update_roster(["host", "returning"])
    returning = binder.resolve(4)
    assert not isinstance(returning, str)
    assert (returning.label, returning.device) == ("C", "returning")


def test_binder_departure_retires_oldest_unbound_slot() -> None:
    binder = SlotBinder()
    binder.update_roster(["a", "b", "c"])
    first = binder.resolve(3)
    second = binder.resolve(4)
    assert not isinstance(first, str)
    assert not isinstance(second, str)
    assert first.device is None
    assert second.device is None
    binder.update_roster(["a", "b"])
    replaced = binder.resolve(3)
    assert not isinstance(replaced, str)
    assert replaced.label == "C"
    kept = binder.resolve(4)
    assert not isinstance(kept, str)
    assert kept.label == "B"


def test_binder_refuses_more_slots_than_unbound_devices() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    assert not isinstance(binder.resolve(3), str)
    assert binder.resolve(4) == "slot_conflict"


def test_binder_refuses_labels_without_roster() -> None:
    binder = SlotBinder()
    assert binder.resolve(3) == "no_roster"


def test_binder_drops_tenures_while_contended() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    assert not isinstance(binder.resolve(3), str)
    binder.update_roster([f"d{index}" for index in range(MEET_AUDIO_SLOTS + 1)])
    binder.update_roster(["host"])
    fresh = binder.resolve(3)
    assert not isinstance(fresh, str)
    assert fresh.label == "B"


def test_binder_never_starts_a_label_while_twin_speaks() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    assert binder.resolve(3, allow_new=False) == "self_overlap"
    assert not isinstance(binder.resolve(3), str)
    known = binder.resolve(3, allow_new=False)
    assert not isinstance(known, str)
    assert known.label == "A"


def test_binder_fleet_capacity_binds_three_remote_streams_stably() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS

    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    binder.update_roster(["remote-a", "remote-b", "remote-c"])
    binder.observe_stream(0, [111])
    binder.observe_stream(1, [222])
    binder.observe_stream(2, [333])
    labels = []
    for slot in (0, 1, 2):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 3
    for slot in (0, 1, 2):
        repeat = binder.resolve(slot)
        assert not isinstance(repeat, str)
        assert repeat.label == labels[slot]


def test_binder_three_roster_binds_without_contention() -> None:
    binder = SlotBinder()
    binder.update_roster([f"remote-{index}" for index in range(MEET_AUDIO_SLOTS)])
    binder.observe_stream(0, [111])
    binder.observe_stream(1, [222])
    binder.observe_stream(2, [333])
    labels = []
    for slot in (0, 1, 2):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == MEET_AUDIO_SLOTS


def test_binder_rejoin_preserves_bound_identity_across_slot_move() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    binder.observe_stream(0, [111])
    first = binder.resolve(0)
    assert not isinstance(first, str)
    assert (first.label, first.device) == ("A", "host")
    binder.update_roster([])
    binder.update_roster(["host"])
    binder.observe_stream(1, [111])
    moved = binder.resolve(1)
    assert not isinstance(moved, str)
    assert moved.label == "A"


def test_binder_keeps_active_speakers_when_lurkers_overflow_roster() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    first = binder.resolve(3)
    assert not isinstance(first, str)
    binder.update_roster(["host", "guest"])
    second = binder.resolve(4)
    assert not isinstance(second, str)
    binder.update_roster(["host", "guest", "lurker-a", "lurker-b", "lurker-c", "lurker-d"])
    kept_first = binder.resolve(3)
    kept_second = binder.resolve(4)
    assert not isinstance(kept_first, str)
    assert not isinstance(kept_second, str)
    assert (kept_first.label, kept_second.label) == (first.label, second.label)


def test_binder_allows_new_speakers_while_roster_overflows_capacity() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    first = binder.resolve(3)
    assert not isinstance(first, str)
    assert first.label == "A"
    binder.update_roster(["host"] + [f"lurker-{index}" for index in range(MEET_AUDIO_SLOTS + 1)])
    outcome = binder.resolve(9)
    assert not isinstance(outcome, str)
    assert outcome.label == "B"
    kept = binder.resolve(3)
    assert not isinstance(kept, str)
    assert kept.label == "A"


def test_binder_distinct_sync_sources_keep_per_slot_labels_without_follow() -> None:
    binder = SlotBinder()
    binder.update_roster(["host", "guest", "visitor"])
    binder.observe_stream(0, [6666])
    binder.observe_stream(1, [6667])
    binder.observe_stream(2, [6668])
    first = binder.resolve(0)
    second = binder.resolve(1)
    third = binder.resolve(2)
    assert not isinstance(first, str)
    assert not isinstance(second, str)
    assert not isinstance(third, str)
    assert len({first.label, second.label, third.label}) == 3
    kept_first = binder.resolve(0)
    kept_second = binder.resolve(1)
    kept_third = binder.resolve(2)
    assert not isinstance(kept_first, str)
    assert not isinstance(kept_second, str)
    assert not isinstance(kept_third, str)
    assert kept_first.label == first.label
    assert kept_second.label == second.label
    assert kept_third.label == third.label


def test_binder_binds_four_streams_when_five_share_the_roster() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS

    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    binder.update_roster([f"devices/{index}" for index in range(5)])
    for slot in range(4):
        binder.observe_stream(slot, [111 + slot])
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 4
    for slot in range(4):
        repeat = binder.resolve(slot)
        assert not isinstance(repeat, str)
        assert repeat.label == labels[slot]


def test_binder_never_binds_self_entry_to_a_speaking_slot() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS

    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    binder.update_roster(["devices/self"] + [f"devices/{index}" for index in range(4)])
    for slot in range(4):
        binder.observe_stream(slot, [111 + slot])
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        assert outcome.device != "devices/self"
        labels.append(outcome.label)
    assert len(set(labels)) == 4


def test_binder_binds_speakers_with_lurker_in_roster() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS

    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    binder.update_roster([f"devices/{index}" for index in range(5)] + ["devices/lurker"])
    for slot in range(4):
        binder.observe_stream(slot, [111 + slot])
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 4
    for slot in range(4):
        repeat = binder.resolve(slot)
        assert not isinstance(repeat, str)
        assert repeat.label == labels[slot]


def test_binder_departure_releases_only_one_tenure() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS

    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    binder.update_roster([f"devices/{index}" for index in range(5)])
    for slot in range(4):
        binder.observe_stream(slot, [111 + slot])
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 4
    binder.update_roster([f"devices/{index}" for index in range(4)])
    kept = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        kept.append(outcome.label)
    assert kept[1:] == labels[1:]
    assert kept[0] != labels[0]


def test_binder_refuses_new_slot_when_demand_reaches_capacity() -> None:
    binder = SlotBinder()
    binder.update_roster(["host", "guest", "visitor"])
    for slot in (3, 4, 5):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
    assert binder.resolve(6) == "slot_contention"
    kept = binder.resolve(3)
    assert not isinstance(kept, str)
    assert kept.label == "A"


def test_binder_refuses_follow_when_shared_label_spans_two_slots() -> None:
    binder = SlotBinder(labels=iter(["A", "A", "B", "C"]))
    binder.update_roster(["d1", "d2", "d3"])
    first = binder.resolve(0)
    second = binder.resolve(1)
    assert not isinstance(first, str)
    assert not isinstance(second, str)
    assert (first.label, second.label) == ("A", "A")
    binder.observe_stream(0, [111])
    binder.observe_stream(1, [111])
    binder.observe_stream(2, [111])
    outcome = binder.resolve(2)
    if isinstance(outcome, str):
        assert outcome in ("slot_conflict", "slot_contention", "self_overlap")
    else:
        assert outcome.label == "B"


def test_binder_emits_minted_event_on_new_label_only() -> None:
    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    first = binder.resolve(0)
    assert not isinstance(first, str)
    assert binder.drain_binder_events() == [{"event": "tenure_minted", "label": "A", "slot": 0}]
    kept = binder.resolve(0)
    assert not isinstance(kept, str)
    assert binder.drain_binder_events() == []


def test_binder_emits_move_event_on_csrc_follow() -> None:
    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    binder.observe_stream(0, [777])
    first = binder.resolve(0)
    assert not isinstance(first, str)
    assert binder.drain_binder_events() == [{"event": "tenure_minted", "label": "A", "slot": 0}]
    binder.observe_stream(1, [777])
    moved = binder.resolve(1)
    assert not isinstance(moved, str)
    assert moved.label == first.label
    events = binder.drain_binder_events()
    assert events == [
        {
            "event": "tenure_moved",
            "label": first.label,
            "from_slot": 0,
            "to_slot": 1,
            "csrcs": [777],
        }
    ]
    assert binder.drain_binder_events() == []


def test_binder_emits_release_event_on_departure() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    host = binder.resolve(3)
    assert not isinstance(host, str)
    assert binder.drain_binder_events() == [{"event": "tenure_minted", "label": "A", "slot": 3}]
    binder.update_roster([])
    events = binder.drain_binder_events()
    assert events == [
        {"event": "tenure_released", "label": "A", "slot": 3, "device": "host", "dormant": True}
    ]


def test_binder_flags_relabel_when_delete_one_releases_present_slot() -> None:
    binder = SlotBinder()
    binder.update_roster(["a", "b", "c"])
    first = binder.resolve(3)
    assert not isinstance(first, str)
    assert binder.drain_binder_events() == [{"event": "tenure_minted", "label": "A", "slot": 3}]
    binder.update_roster(["a", "b"])
    release = binder.drain_binder_events()
    assert release == [
        {"event": "tenure_released", "label": "A", "slot": 3, "device": None, "dormant": False}
    ]
    replaced = binder.resolve(3)
    assert not isinstance(replaced, str)
    assert replaced.label == "B"
    fault = binder.drain_binder_events()
    assert fault == [
        {"event": "tenure_minted", "label": "B", "slot": 3},
        {"event": "tenure_relabel", "slot": 3, "old_label": "A", "new_label": "B"},
    ]


def test_binder_reset_clears_labels_and_events() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    first = binder.resolve(0)
    assert not isinstance(first, str)
    binder.update_roster([])
    binder.reset()
    binder.update_roster(["host"])
    outcome = binder.resolve(0)
    assert not isinstance(outcome, str)
    assert binder.drain_binder_events() == [{"event": "tenure_minted", "label": "B", "slot": 0}]


def test_binder_defers_dormant_restore_while_slot_occupied() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    host = binder.resolve(0)
    assert not isinstance(host, str)
    assert host.device == "host"
    binder.update_roster(["host", "guest"])
    assert binder.drain_binder_events() == [{"event": "tenure_minted", "label": "A", "slot": 0}]
    binder.update_roster(["guest"])
    assert binder.drain_binder_events() != []
    other = binder.resolve(0)
    assert not isinstance(other, str)
    assert other.label != host.label
    binder.update_roster(["guest", "host"])
    assert binder.resolve(0) is other
    binder.update_roster(["host"])
    restored = binder.resolve(0)
    assert not isinstance(restored, str)
    assert restored.label == host.label


def test_binder_adapter_exposes_registry_uuid() -> None:
    from fakeyou.identity.identity import SpeakerRegistry

    registry = SpeakerRegistry()
    registry.update_roster(["dev-a"])
    binder = SlotBinder(registry=registry)
    outcome = binder.resolve(0)
    assert isinstance(outcome, Tenure)
    direct = registry.resolve(0)
    assert not isinstance(direct, str)
    assert outcome.uuid == direct.uuid
    assert outcome.uuid


def test_binder_adapter_keeps_default_registry_private() -> None:
    first = SlotBinder()
    second = SlotBinder()
    first.update_roster(["dev-a"])
    second.update_roster(["dev-a"])
    first_row = first.resolve(0)
    second_row = second.resolve(0)
    assert isinstance(first_row, Tenure)
    assert isinstance(second_row, Tenure)
    assert first_row.uuid == second_row.uuid
    assert first.drain_binder_events() != []


def test_tracker_and_binder_share_registry_rows() -> None:
    from fakeyou.identity.identity import SpeakerRegistry

    registry = SpeakerRegistry()
    registry.update_roster(["dev-a", "dev-b"])
    binder = SlotBinder(registry=registry)
    clock = FakeClock(now=10.0)
    tracker = VoiceActivityTracker(monotonic=clock.monotonic, registry=registry)
    binder.observe_stream(0, [99])
    assert isinstance(binder.resolve(0), Tenure)
    binder.observe_stream(1, [11])
    row = binder.resolve(1)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(11_000.0, [[10_300.0, 0, 0.4, [11]]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    attribution = tracker.attribute()
    assert isinstance(row, Tenure)
    assert attribution.reason == "dominant_csrc"
    assert attribution.participant_id == "speaker-b"
    assert attribution.speaker_uuid == row.uuid
