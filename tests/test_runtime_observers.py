import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from fakeyou.meet.meet import DEFAULT_MEET_SIGNALS
from fakeyou.meet.participant_observer import OBSERVE_JS
from fakeyou.orchestration.runtime import MeetingWorker, SessionWiring, run_meeting_session
from fakeyou.perception.meet_protocol import MeetUser
from fakeyou.perception.roster import MeetRoster
from tests._audio_fakes import FakeAudio
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import ENDED, IN_CALL, FakePageSession, meet_page
from tests._persistence_fakes import FakeSaverSession, RecordingJournal
from tests._runtime_support import mining_payload, run_session, session_config


async def test_run_meeting_session_collects_participant_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "meeting-1" / "participants.jsonl"
    page = meet_page(
        [IN_CALL, IN_CALL, IN_CALL, IN_CALL, ENDED],
        evaluates=[mining_payload() for _ in range(20)],
    )
    browser = FakePageSession(page)
    journal = RecordingJournal()
    clock = FakeClock()

    result = await run_meeting_session(
        session_config(participant_policy_enabled=True, participant_evidence_path=evidence),
        SessionWiring(
            open_page=browser.open,
            saver_factory=FakeSaverSession().factory,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
        ),
    )

    assert result.phase == "ended"
    assert OBSERVE_JS in page.evaluate_calls
    lines = evidence.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1
    record = json.loads(lines[0])
    assert record["method"] == "dom_mining"
    assert record["epoch"] == "meeting-1:e1"
    assert record["tiles"][0]["key"] == "a"
    started = journal.events("participant_observer_started")
    assert len(started) == 1
    assert "Budi" not in json.dumps(started)
    calls_after_finish = len(page.evaluate_calls)
    await asyncio.sleep(0.6)
    assert len(page.evaluate_calls) == calls_after_finish


async def test_run_meeting_session_skips_observer_when_flag_off(tmp_path: Path) -> None:
    evidence = tmp_path / "meeting-1" / "participants.jsonl"
    page = meet_page([IN_CALL, IN_CALL, ENDED])
    browser = FakePageSession(page)

    result = await run_session(
        session_config(participant_evidence_path=evidence), browser, FakeSaverSession(), FakeClock()
    )

    assert result.phase == "ended"
    assert OBSERVE_JS not in page.evaluate_calls
    assert not evidence.exists()


async def test_owner_stop_produces_no_observer_or_audio_followups(tmp_path: Path) -> None:
    evidence = tmp_path / "meeting-1" / "participants.jsonl"
    stop = asyncio.Event()
    clock = FakeClock()
    payloads = []
    for index in range(10):
        payload = mining_payload()
        payload["tiles"][0]["label"] = f"Budi-{index % 2}"
        payloads.append(payload)
    page = meet_page([], evaluates=payloads)
    worker = MeetingWorker(
        session_config(participant_policy_enabled=True, participant_evidence_path=evidence),
        page,
        stop,
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
    )
    worker.audio = FakeAudio([])

    await worker.start_participant_observer()
    assert worker.observer is not None
    await asyncio.sleep(0.6)
    assert evidence.exists()
    assert len(evidence.read_text(encoding="utf-8").splitlines()) >= 2

    stop.set()
    detail = await worker.run_monitor(clock.monotonic())

    assert detail == "owner requested stop"
    assert worker.observer is None
    assert worker.audio.events == ["audio_stopped"]
    lines_before = evidence.read_text(encoding="utf-8").splitlines()
    polls_before = len(page.evaluate_calls)
    await asyncio.sleep(0.6)
    assert evidence.read_text(encoding="utf-8").splitlines() == lines_before
    assert len(page.evaluate_calls) == polls_before
    assert worker.audio.events == ["audio_stopped"]


class _KeywordAudio(FakeAudio):
    def __init__(self) -> None:
        super().__init__([])
        self.keywords: list[list[str]] = []

    def update_keywords(self, terms: Sequence[str]) -> bool:
        self.keywords.append(list(terms))
        return True


def test_roster_names_bias_the_transcriber() -> None:
    clock = FakeClock()
    journal = RecordingJournal()
    worker = MeetingWorker(
        session_config(),
        meet_page([]),
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
        journal=journal,
    )
    audio = _KeywordAudio()
    worker.audio = audio
    roster = MeetRoster()
    roster.apply_users(
        [
            MeetUser(device_id="devices/ayu", name="Ayu", present=True),
            MeetUser(device_id="devices/budi", name="Budi", present=True),
            MeetUser(device_id="devices/twin", name="Rani Twin", present=True, current_user=True),
        ]
    )

    worker.bias_transcriber(roster)

    assert audio.keywords == [["Ayu", "Budi"]]
    [recorded] = journal.events("stt_keywords_recorded")
    assert recorded["keywords"] == 2
    assert "Ayu" not in json.dumps(recorded)
