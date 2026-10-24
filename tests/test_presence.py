from fakeyou.orchestration.presence import PROTOCOL_STALE_SECONDS, AloneClock
from fakeyou.perception.roster import MeetingState

LEAVE = 600.0


def state(*names: str, synced: bool = True) -> MeetingState:
    return MeetingState(
        people={f"devices/{name}": name for name in names},
        presenter=None,
        cameras_on=frozenset(),
        synced=synced,
    )


def test_the_twin_leaves_after_the_protocol_shows_nobody_for_the_whole_window() -> None:
    clock = AloneClock(leave_after_seconds=LEAVE)

    for second in range(0, int(LEAVE) + 1, 15):
        reading = clock.observe(float(second), state=state(), syncs=second + 1, dom_remote=None)

    assert reading.source == "protocol"
    assert clock.due(LEAVE)


def test_failed_dom_readings_never_restart_the_window() -> None:
    clock = AloneClock(leave_after_seconds=LEAVE)
    clock.observe(0.0, state=state(), syncs=1, dom_remote=0)

    for second in range(15, int(LEAVE) + 1, 15):
        clock.observe(float(second), state=state(), syncs=second, dom_remote=None)

    assert clock.due(LEAVE)


def test_someone_joining_resets_the_window() -> None:
    clock = AloneClock(leave_after_seconds=LEAVE)
    clock.observe(0.0, state=state(), syncs=1, dom_remote=None)
    clock.observe(300.0, state=state("Ayu"), syncs=2, dom_remote=None)
    clock.observe(310.0, state=state(), syncs=3, dom_remote=None)

    assert not clock.due(LEAVE)
    assert clock.due(310.0 + LEAVE)


def test_the_dom_seeing_people_vetoes_an_empty_protocol_roster_as_drift() -> None:
    clock = AloneClock(leave_after_seconds=LEAVE)

    reading = clock.observe(0.0, state=state(), syncs=1, dom_remote=2)

    assert reading.drift
    assert reading.remote == 2
    assert not clock.due(LEAVE)


def test_a_stale_protocol_falls_back_to_the_dom() -> None:
    clock = AloneClock(leave_after_seconds=LEAVE)
    clock.observe(0.0, state=state("Ayu"), syncs=1, dom_remote=None)

    reading = clock.observe(PROTOCOL_STALE_SECONDS + 1, state=state("Ayu"), syncs=1, dom_remote=0)

    assert reading.source == "dom"
    assert reading.remote == 0


def test_without_any_signal_the_clock_neither_starts_nor_restarts() -> None:
    clock = AloneClock(leave_after_seconds=LEAVE)

    reading = clock.observe(0.0, state=None, syncs=0, dom_remote=None)

    assert reading.source == "unknown"
    assert not clock.due(10 * LEAVE)


def test_an_unsynced_roster_is_not_trusted() -> None:
    clock = AloneClock(leave_after_seconds=LEAVE)

    reading = clock.observe(0.0, state=state(synced=False), syncs=0, dom_remote=None)

    assert reading.source == "unknown"
