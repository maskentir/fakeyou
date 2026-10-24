import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from fakeyou.config.settings import PerceptionSettings, Settings
from fakeyou.conversation.state import LifecycleState
from fakeyou.meet.meet import DEFAULT_MEET_SIGNALS, JoinResult
from fakeyou.meet.participant_observer import ParticipantEvidenceWriter
from fakeyou.observability.faults import FaultReporter
from fakeyou.orchestration.lifecycle import JoinAttempt, initial_lifecycle_state
from fakeyou.orchestration.perception import create_perception_stack
from fakeyou.orchestration.runtime import MeetingWorker
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import FakePage, meet_page
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
