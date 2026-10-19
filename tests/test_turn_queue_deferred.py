from datetime import UTC, datetime

from fakeyou.conversation.turn_queue import DeferredTurn, DeferredTurns, QuietFloor
from fakeyou.foundation.transcript import TranscriptEvent
from tests._fakes_common import FakeClock


def _turn(event_id: str, *, expires_at: float = 10.0, quiet: bool = False) -> DeferredTurn:
    event = TranscriptEvent(
        event_id,
        datetime.now(UTC).isoformat(),
        "meeting_attention",
        "noticed",
        finalized=True,
        participant_id="unknown",
        speaker_evidence_id=None,
    )
    return DeferredTurn(
        event=event, channel="voice", expires_at=expires_at, needs_quiet_floor=quiet
    )


def test_deferred_turns_are_unique_by_event() -> None:
    turns = DeferredTurns()

    assert turns.add(_turn("a1"))
    assert not turns.add(_turn("a1"))
    assert turns.holds("a1")


def test_the_first_ready_turn_is_taken_in_order() -> None:
    turns = DeferredTurns()
    turns.add(_turn("quiet", quiet=True))
    turns.add(_turn("chat"))

    taken = turns.next_ready(lambda turn: not turn.needs_quiet_floor)

    assert taken is not None
    assert taken.event.id == "chat"
    assert turns.holds("quiet")
    assert turns.next_ready(lambda _turn: False) is None


def test_expired_turns_are_removed_and_returned() -> None:
    turns = DeferredTurns()
    turns.add(_turn("old", expires_at=1.0))
    turns.add(_turn("new", expires_at=5.0))

    expired = turns.expire(2.0)

    assert [turn.event.id for turn in expired] == ["old"]
    assert turns.earliest_expiry() == 5.0


def test_a_preempted_turn_is_retried_once_at_the_front() -> None:
    turns = DeferredTurns()
    turns.add(_turn("other"))
    first = _turn("a1")

    assert turns.retry(first)
    again = turns.next_ready(lambda _turn: True)
    assert again is not None
    assert (again.event.id, again.attempts) == ("a1", 1)
    assert not turns.retry(again)


def test_clear_empties_the_lane() -> None:
    turns = DeferredTurns()
    turns.add(_turn("a1"))
    turns.clear()

    assert not turns.pending
    assert turns.earliest_expiry() is None


def test_the_quiet_floor_measures_time_since_the_last_activity() -> None:
    clock = FakeClock()
    floor = QuietFloor(clock=clock.monotonic)

    assert floor.quiet_for() == float("inf")
    floor.note_activity()
    clock.now += 1.5

    assert floor.quiet_for() == 1.5
