from typing import Any

from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.identity.identity import SpeakerRegistry
from tests._capture_support import recording_registry
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import meet_page


def _collision_payload(names: list[str]) -> dict[str, Any]:
    return {
        "streams": [
            {
                "slot": slot,
                "seq": 0,
                "csrcs": [111 + slot],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
            for slot in range(4)
        ],
        "roster": [{"id": f"devices/{index}", "name": name} for index, name in enumerate(names)],
        "dropped": 0,
    }


async def test_capture_drain_binds_three_remote_streams_with_self_present() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.audio.voice_activity import SlotBinder

    payload = {
        "streams": [
            {
                "slot": slot,
                "seq": 0,
                "csrcs": [111 + slot],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
            for slot in range(3)
        ],
        "roster": [
            {"id": "devices/1", "name": "Host"},
            {"id": "devices/2", "name": "Guest"},
            {"id": "devices/3", "name": "Visitor"},
            {"id": "devices/9", "name": "Twin"},
        ],
        "dropped": 0,
    }
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    labels = []
    for slot in range(3):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 3


async def test_capture_drain_binds_four_streams_when_twin_names_collide() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.audio.voice_activity import SlotBinder

    payload = _collision_payload(["Twin"] * 5)
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    assert binder.roster_size == 5
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


async def test_capture_drain_binds_speakers_with_lurker_present() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.audio.voice_activity import SlotBinder

    payload = _collision_payload(["Twin"] * 4 + ["Host"])
    roster = list(payload["roster"])
    roster.append({"id": "devices/lurker", "name": "Lurker"})
    payload["roster"] = roster
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    assert binder.roster_size == 6
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


async def test_capture_drain_binds_four_humans_with_unique_names() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.audio.voice_activity import SlotBinder

    payload = _collision_payload(["Host", "Guest", "Visitor", "Partner", "Twin"])
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    assert binder.roster_size == 4
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        assert outcome.device != "devices/4"
        labels.append(outcome.label)
    assert len(set(labels)) == 4


async def test_capture_poll_binds_display_claim_from_roster() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    payload = {
        "streams": [
            {
                "slot": 0,
                "seq": 0,
                "csrcs": [111],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
        ],
        "roster": [
            {"id": "dev-x", "muted": False, "name": "Harun"},
            {"id": "dev-t", "muted": False, "name": "Rani Twin"},
        ],
        "dropped": 0,
    }
    page = meet_page([], evaluates=[payload, None])
    speakers = SpeakerRegistry()
    binder = SlotBinder(registry=speakers)
    registry, _ = recording_registry(FakeClock().monotonic, binder=binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Rani Twin")
    assert await loop.poll() is True
    row = speakers.resolve(0)
    assert not isinstance(row, str)
    assert row.device == "dev-x"
    assert row.display_claim == "Harun"
    assert row.claim_confidence == 1.0


async def test_capture_drain_leaves_colliding_self_entry_unbound() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.audio.voice_activity import SlotBinder

    payload = _collision_payload(["Twin"] * 4 + ["Host"])
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    assert binder.roster_size == 5
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        assert outcome.device != "devices/0"
