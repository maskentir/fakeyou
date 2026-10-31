import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from fakeyou.config.settings import PerceptionSettings, Settings
from fakeyou.conversation.state import LifecycleState
from fakeyou.meet.meet import DEFAULT_MEET_SIGNALS, CallObservation, JoinResult
from fakeyou.meet.participant_observer import ParticipantEvidenceWriter
from fakeyou.meet.watchdog import WatchdogCheck
from fakeyou.observability.faults import FaultReporter
from fakeyou.orchestration.call_state import DRAIN_STALE_SECONDS
from fakeyou.orchestration.lifecycle import JoinAttempt, initial_lifecycle_state
from fakeyou.orchestration.perception import create_perception_stack
from fakeyou.orchestration.runtime import MeetingWorker, MonitorScan
from fakeyou.perception.meet_protocol import MeetUser
from fakeyou.perception.roster import MeetRoster
from fakeyou.perception.sensors import DrainBatch
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import ENDED, IN_CALL, REMOVED, FakePage, meet_page
from tests._persistence_fakes import RecordingJournal
from tests._runtime_support import mining_payload, session_config


def _bare_worker(**overrides: Any) -> MeetingWorker:
    clock = FakeClock()
    return MeetingWorker(
        session_config(**overrides),
        meet_page([]),
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
    )


async def _wait_for_faults(reporter: FaultReporter, count: int = 1) -> None:
    for _ in range(200):
        if reporter.total() >= count:
            return
        await asyncio.sleep(0.01)


async def test_monitor_propagates_unexpected_starter_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _bare_worker()

    async def boom() -> None:
        raise ValueError("bug")

    monkeypatch.setattr(worker, "start_voice_activity", boom)
    with pytest.raises(ValueError, match="bug"):
        await worker.run_monitor(0.0)


async def test_monitor_propagates_unexpected_capture_start_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _bare_worker()

    async def boom(*, enabled: bool) -> None:
        del enabled
        raise ValueError("bug")

    monkeypatch.setattr(worker, "start_capture_drain", boom)
    with pytest.raises(ValueError, match="bug"):
        await worker.run_monitor(0.0)


async def test_disable_camera_best_effort_propagates_unexpected_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = _bare_worker()

    async def boom(*args: Any) -> bool:
        del args
        raise ValueError("bug")

    monkeypatch.setattr("fakeyou.orchestration.runtime.disable_meeting_camera", boom)
    with pytest.raises(ValueError, match="bug"):
        await worker.disable_camera_best_effort()


async def test_participant_evidence_writer_bug_surfaces_as_poll_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "evidence.jsonl"
    clock = FakeClock()
    worker = MeetingWorker(
        session_config(participant_policy_enabled=True, participant_evidence_path=evidence),
        meet_page([], evaluates=[mining_payload() for _ in range(10)]),
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
    )

    def boom_write(raw: Any, mono_ms: float) -> bool:
        del mono_ms
        del raw
        raise ValueError("bug")

    monkeypatch.setattr(ParticipantEvidenceWriter, "write", boom_write)
    await worker.start_participant_observer()
    assert worker.observer is not None
    await asyncio.sleep(0.6)
    failures = worker.observer.poll_failures
    await worker.close_participant_observer()

    assert failures >= 1


async def test_start_voice_activity_records_poll_faults(journal: RecordingJournal) -> None:
    from fakeyou.audio.voice_activity import VoiceActivityTracker

    reporter = FaultReporter(journal=journal)
    worker = _bare_worker()
    worker.faults = reporter
    worker.voice_activity = VoiceActivityTracker()
    await worker.start_voice_activity()
    try:
        await _wait_for_faults(reporter)
        assert reporter.total() >= 1
    finally:
        await worker.close_voice_activity()


async def test_start_participant_observer_records_poll_faults(
    journal: RecordingJournal, tmp_path: Path
) -> None:
    reporter = FaultReporter(journal=journal)
    worker = _bare_worker(
        participant_policy_enabled=True,
        participant_evidence_path=tmp_path / "evidence.jsonl",
    )
    worker.faults = reporter
    await worker.start_participant_observer()
    try:
        await _wait_for_faults(reporter)
        assert reporter.total() >= 1
    finally:
        await worker.close_participant_observer()


async def test_start_capture_drain_records_poll_faults(journal: RecordingJournal) -> None:
    reporter = FaultReporter(journal=journal)
    worker = _bare_worker()
    worker.faults = reporter
    await worker.start_capture_drain(enabled=True)
    try:
        await _wait_for_faults(reporter)
        assert reporter.total() >= 1
    finally:
        await worker.close_capture_drain()


async def test_start_perception_drains_the_page_and_listens_for_roster_syncs(
    journal: RecordingJournal,
) -> None:
    reporter = FaultReporter(journal=journal)
    worker = _bare_worker()
    worker.faults = reporter
    worker.journal = journal
    worker.perception = create_perception_stack(
        Settings(perception=PerceptionSettings(enabled=True)), journal=journal
    )
    page = worker.page
    assert isinstance(page, FakePage)
    await worker.start_perception()
    try:
        await _wait_for_faults(reporter)
        assert reporter.total() >= 1
        assert len(page.listeners["response"]) == 1
    finally:
        await worker.close_perception()
    assert page.listeners["response"] == []
    assert [row["event"] for row in journal.events() if "perception_drain" in row["event"]] == [
        "perception_drain_started",
        "perception_drain_stopped",
    ]


async def test_roster_syncs_are_heard_from_the_first_join_attempt(
    journal: RecordingJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = _bare_worker()
    worker.journal = journal
    worker.perception = create_perception_stack(
        Settings(perception=PerceptionSettings(enabled=True)), journal=journal
    )
    page = worker.page
    assert isinstance(page, FakePage)
    heard: list[int] = []

    async def attempt() -> JoinResult:
        heard.append(len(page.listeners.get("response", [])))
        return JoinResult(status="admitted", detail="guest admitted to the call")

    monkeypatch.setattr(worker, "_attempt_guest_join", attempt)
    created = initial_lifecycle_state("owner", "twin", "meeting-1")
    prepared = cast(LifecycleState, {**created, **await worker.prepare(created)})
    await worker.join(prepared, JoinAttempt(number=1, last=False))
    await worker.start_perception()
    try:
        assert heard == [1]
        assert len(page.listeners["response"]) == 1
    finally:
        await worker.close_perception()
    assert page.listeners["response"] == []


class _Sensors:
    def __init__(self) -> None:
        self.roster = MeetRoster()
        self.roster.mark_synced()
        self.heartbeats = 1


async def test_the_twin_leaves_an_empty_meeting_without_audio_while_the_dom_fails(
    journal: RecordingJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    worker = MeetingWorker(
        session_config(),
        meet_page([]),
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
        journal=journal,
    )
    sensors = _Sensors()
    worker.perception = cast(Any, SimpleNamespace(sensors=sensors, close=_closed))
    ended: list[str] = []

    async def finish(kind: str, _attribution: str, detail: str) -> str:
        ended.append(kind)
        return detail

    monkeypatch.setattr(worker, "finish", finish)
    scan = MonitorScan(next_scan=clock.now, remote_participants=None, presence_at=clock.now)
    finished = None
    for _ in range(int(worker.config.alone_leave_seconds // 15) + 2):
        sensors.heartbeats += 1
        _remote, finished = await worker.check_presence(scan)
        if finished is not None:
            break
        await clock.sleep(15)

    assert ended == ["participants_left"]
    assert finished is not None
    assert "protocol" in finished
    assert worker.audio is None


async def test_the_dom_seeing_people_keeps_the_twin_in_and_records_drift(
    journal: RecordingJournal,
) -> None:
    clock = FakeClock()
    worker = MeetingWorker(
        session_config(),
        meet_page([]),
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
        journal=journal,
    )
    sensors = _Sensors()
    worker.perception = cast(Any, SimpleNamespace(sensors=sensors, close=_closed))
    scan = MonitorScan(next_scan=clock.now, remote_participants=2, presence_at=clock.now)
    for _ in range(int(worker.config.alone_leave_seconds // 15) + 2):
        sensors.heartbeats += 1
        scan.presence_at = clock.now
        _remote, finished = await worker.check_presence(scan)
        assert finished is None
        await clock.sleep(15)

    [drift] = journal.events("presence_drift_observed")
    assert (drift["protocol"], drift["dom"]) == (0, 2)


async def _closed() -> None:
    return None


async def test_a_failing_dom_does_not_end_a_session_whose_protocol_is_healthy(
    journal: RecordingJournal, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    worker = MeetingWorker(
        session_config(),
        meet_page([]),
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
        journal=journal,
    )
    sensors = _Sensors()
    worker.perception = cast(Any, SimpleNamespace(sensors=sensors, close=_closed))
    scan = MonitorScan(next_scan=clock.now, remote_participants=None, presence_at=clock.now)
    ended: list[str] = []

    async def finish(kind: str, _attribution: str, detail: str) -> str:
        ended.append(kind)
        return detail

    monkeypatch.setattr(worker, "finish", finish)
    failing = WatchdogCheck(
        observation=CallObservation(state="unknown", detail="scan failed"),
        events=(),
        observation_failed=True,
        participant_count=None,
        next_check_seconds=5.0,
    )

    for _ in range(40):
        sensors.heartbeats += 1
        await worker.check_presence(scan)
        assert await worker.track_observation(scan, failing) is None
        await clock.sleep(15)

    assert ended == []
    assert journal.events("ui_observation_expired")


CHAT_THAT_SOUNDS_FINAL = (
    '<button aria-label="Leave call"></button>'
    '<div role="list"><div>the sprint has ended</div></div>'
)


async def _protocol_worker(
    journal: RecordingJournal, clock: FakeClock, *, status: int, peer: str
) -> MeetingWorker:
    worker = _bare_worker()
    worker.monotonic = clock.monotonic
    worker.journal = journal
    stack = create_perception_stack(
        Settings(perception=PerceptionSettings(enabled=True)), journal=journal
    )
    assert stack is not None
    worker.perception = stack
    stack.sensors.monotonic = clock.monotonic
    stack.sensors.roster.apply_users(
        [
            MeetUser(
                device_id="devices/twin",
                name="Rani",
                present=status == 1,
                status=status,
                current_user=True,
            )
        ]
    )
    await stack.sensors.accept_batch(DrainBatch(peers=[peer]))
    return worker


async def test_a_fresh_protocol_says_in_call_without_reading_the_page(
    journal: RecordingJournal,
) -> None:
    worker = await _protocol_worker(journal, FakeClock(), status=1, peer="connected")

    observation = await worker.observe_call(meet_page([]), DEFAULT_MEET_SIGNALS)

    assert (observation.state, observation.detail) == ("in_call", "own device is in the meeting")


async def test_a_stale_protocol_falls_back_to_the_scoped_page_probe(
    journal: RecordingJournal,
) -> None:
    clock = FakeClock()
    worker = await _protocol_worker(journal, clock, status=1, peer="connected")
    assert worker.protocol_call_state() is not None
    clock.now += DRAIN_STALE_SECONDS + 1

    chat = await worker.observe_call(meet_page([CHAT_THAT_SOUNDS_FINAL]), DEFAULT_MEET_SIGNALS)
    removed = await worker.observe_call(meet_page([REMOVED]), DEFAULT_MEET_SIGNALS)

    assert chat.state == "in_call"
    assert removed.state == "removed"


async def test_an_own_device_out_of_the_meeting_takes_its_reason_from_the_end_screen(
    journal: RecordingJournal,
) -> None:
    worker = await _protocol_worker(journal, FakeClock(), status=6, peer="closed")

    named = await worker.observe_call(meet_page([ENDED]), DEFAULT_MEET_SIGNALS)
    unnamed = await worker.observe_call(meet_page(["<div>blank</div>"]), DEFAULT_MEET_SIGNALS)
    lingering = await worker.observe_call(meet_page([IN_CALL]), DEFAULT_MEET_SIGNALS)

    assert named.state == "ended"
    assert lingering.state == "unknown"
    assert [row["headings"] for row in journal.events("call_end_screen_observed")] == [
        "the call has ended",
        None,
    ]
    assert (unnamed.state, unnamed.detail) == (
        "ended",
        "own device is out of the meeting (status 6)",
    )
