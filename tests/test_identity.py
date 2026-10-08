import ast
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from fakeyou.identity import SpeakerRegistry
from fakeyou.voiceprint_match import VoiceScores

DISPLAY_CLAIM_RENDER_READERS = frozenset({"record.py", "debrief.py"})
DISPLAY_CLAIM_TASK_READERS = frozenset({"verify.py"})


def _registry() -> SpeakerRegistry:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-a", "dev-b"])
    return registry


def _bound_registry() -> SpeakerRegistry:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-a"])
    registry.observe_stream(0, [11])
    registry.resolve(0)
    registry.update_roster(["dev-a", "dev-b"])
    registry.observe_stream(1, [22])
    registry.resolve(1)
    return registry


def _display_claim_reader_files() -> set[str]:
    root = Path(__file__).resolve().parent.parent / "src" / "fakeyou"
    readers = set()
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in {
                "display_claim",
                "claim_confidence",
            }:
                readers.add(path.relative_to(root).as_posix())
    return readers


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


def test_registry_snapshot_uses_noted_voice_margin() -> None:
    registry = _registry()
    registry.observe_stream(0, [11])
    row = registry.resolve(0)
    assert not isinstance(row, str)

    registry.note_voice_scores(row.uuid, VoiceScores("alice", 0.75, 0.25, "verified"))

    updated = registry.resolve(0)
    assert not isinstance(updated, str)
    assert updated.margin == 0.5
    assert updated.margin == 0.75 - 0.25


def test_registry_snapshot_keeps_unit_margin_without_scores() -> None:
    registry = _registry()
    registry.observe_stream(0, [11])
    registry.observe_stream(1, [22])
    first = registry.resolve(0)
    second = registry.resolve(1)
    assert not isinstance(first, str)
    assert not isinstance(second, str)

    registry.note_voice_scores(first.uuid, VoiceScores("alice", 0.75, 0.25, "verified"))

    unscored = registry.resolve(1)
    assert not isinstance(unscored, str)
    assert unscored.margin == 1.0


def test_registry_ignores_voice_scores_for_unknown_uuid() -> None:
    registry = _registry()
    registry.observe_stream(0, [11])
    row = registry.resolve(0)
    assert not isinstance(row, str)

    registry.note_voice_scores("0" * 32, VoiceScores("alice", 0.75, 0.25, "verified"))

    unchanged = registry.resolve(0)
    assert not isinstance(unchanged, str)
    assert unchanged.margin == 1.0


def test_registry_binds_display_claim_on_strong_fusion() -> None:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-a"])
    registry.observe_stream(0, [11])
    registry.resolve(0)

    registry.note_roster([{"id": "dev-a", "name": "Maya"}])

    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert row.device == "dev-a"
    assert row.display_claim == "Maya"
    assert row.claim_confidence == 1.0


def test_registry_clears_display_claim_on_rename() -> None:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-a"])
    registry.observe_stream(0, [11])
    registry.resolve(0)
    registry.note_roster([{"id": "dev-a", "name": "Maya"}])
    registry.drain_events()

    registry.note_roster([{"id": "dev-a", "name": "Rani"}])

    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert row.display_claim is None
    assert row.claim_confidence == 0.0
    events = registry.drain_events()
    assert [event["event"] for event in events] == ["display_claim_changed"]
    assert all("verified" not in event for event in events)

    registry.note_roster([{"id": "dev-a", "name": "Maya"}])
    repeat = registry.resolve(0)
    assert not isinstance(repeat, str)
    assert repeat.display_claim is None


def test_registry_nulls_display_claim_on_duplicate_names() -> None:
    registry = _bound_registry()

    registry.note_roster([{"id": "dev-a", "name": "Sam"}, {"id": "dev-b", "name": "Sam"}])

    first = registry.resolve(0)
    second = registry.resolve(1)
    assert not isinstance(first, str)
    assert not isinstance(second, str)
    assert first.display_claim is None
    assert second.display_claim is None
    assert first.claim_confidence == 0.0
    assert second.claim_confidence == 0.0


def test_registry_clears_locked_claim_on_late_duplicate() -> None:
    registry = _bound_registry()
    registry.note_roster([{"id": "dev-a", "name": "Maya"}])
    bound = registry.resolve(0)
    assert not isinstance(bound, str)
    assert bound.display_claim == "Maya"
    assert bound.claim_confidence == 1.0

    registry.note_roster([{"id": "dev-a", "name": "Maya"}, {"id": "dev-b", "name": "Maya"}])
    first = registry.resolve(0)
    second = registry.resolve(1)
    assert not isinstance(first, str)
    assert not isinstance(second, str)
    assert first.display_claim is None
    assert second.display_claim is None
    assert first.claim_confidence == 0.0
    assert second.claim_confidence == 0.0

    registry.note_roster([{"id": "dev-a", "name": "Maya"}])
    repeat = registry.resolve(0)
    assert not isinstance(repeat, str)
    assert repeat.display_claim is None
    assert repeat.claim_confidence == 0.0


def test_registry_keeps_first_display_claim_on_repeated_fusion() -> None:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-a"])
    registry.observe_stream(0, [11])
    registry.resolve(0)
    registry.note_roster([{"id": "dev-a", "name": "Maya"}])
    before = registry.resolve(0)
    registry.drain_events()

    registry.note_roster([{"id": "dev-a", "name": "Maya"}])

    after = registry.resolve(0)
    assert after == before
    assert not isinstance(after, str)
    assert after.display_claim == "Maya"
    assert registry.drain_events() == []


def test_display_claim_has_no_decision_readers() -> None:
    allowed = DISPLAY_CLAIM_RENDER_READERS | DISPLAY_CLAIM_TASK_READERS | {"identity.py"}

    assert _display_claim_reader_files() <= allowed
