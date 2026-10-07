from uuid import NAMESPACE_URL, uuid5

from fakeyou.identity import SpeakerRegistry


def _registry() -> SpeakerRegistry:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-a", "dev-b"])
    return registry


def test_registry_mints_deterministic_uuid_rows() -> None:
    registry = _registry()
    registry.observe_stream(0, [11])
    row = registry.resolve(0)
    assert row.label == "A"
    assert row.uuid == uuid5(NAMESPACE_URL, "speaker:A").hex
    assert row.status == "provisional"
    assert row.confidence == 0.5
    assert row.margin == 1.0
    assert row.slot == 0
    assert row.csrcs == (11,)
    assert row.history[0] == "minted"


def test_registry_follows_csrc_across_slots() -> None:
    registry = _registry()
    registry.observe_stream(0, [11])
    first = registry.resolve(0)
    registry.observe_stream(1, [11])
    moved = registry.resolve(1)
    assert moved.uuid == first.uuid
    assert moved.slot == 1
    assert moved.confidence == 0.7
    assert "moved" in moved.history
    events = registry.drain_events()
    assert [event["event"] for event in events] == ["tenure_minted", "tenure_moved"]
    assert events[1]["from_slot"] == 0
    assert events[1]["to_slot"] == 1


def test_registry_binds_once_and_refuses_conflicts() -> None:
    registry = SpeakerRegistry()
    registry.update_roster(["only"])
    registry.observe_stream(0, [11])
    row = registry.resolve(0)
    assert row.device == "only"
    assert row.status == "confirmed"
    assert row.confidence == 1.0
    assert registry.resolve(0) == row
    registry.observe_stream(1, [22])
    assert registry.resolve(1) == "slot_conflict"


def test_registry_resolve_csrcs_conflicts_on_split_identity() -> None:
    registry = _registry()
    first = registry.resolve_csrcs(frozenset([11]))
    second = registry.resolve_csrcs(frozenset([22]))
    assert first.uuid != second.uuid
    assert registry.resolve_csrcs(frozenset([11, 22])) is None
    assert registry.resolve_csrcs(frozenset([11])) == first
