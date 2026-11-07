import asyncio
import os
import sys
from pathlib import Path

import pytest

from evals._meeting_simulator import FIXTURES, MeetingSimulator, journal_rows, wait_for_event
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import PathSettings, Settings
from fakeyou.memory.persona import OperatorIdentity
from fakeyou.observability.journal import journal_path
from fakeyou.orchestration.join import run_join_session
from fakeyou.orchestration.join_types import JoinPorts, JoinRequest
from fakeyou.orchestration.providers import open_persona_memory
from fakeyou.persistence.record import TwinSpeechEntry, load_meeting_record, meeting_record_path

MEETING_AUDIO_MONITOR = "meeting_audio.monitor"
LINK = "https://meet.google.com/sim-ulat-edm"


def _settings(tmp_path: Path) -> Settings:
    settings = Settings()
    paths = PathSettings(
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        browser_profile_dir=tmp_path / "profiles",
        browser_output_dir=tmp_path / "browser",
    )
    return settings.model_copy(update={"paths": paths})


def _request(settings: Settings, meeting_id: str, duration: float) -> JoinRequest:
    return JoinRequest(
        link=LINK,
        settings=settings,
        identity=OperatorIdentity(
            FIXTURES / "persona.json", SessionId("sim_owner"), SessionId("sim_twin")
        ),
        audio_enabled=True,
        twin_name="Rani Twin",
        meeting_id=meeting_id,
        lobby_timeout=60,
        duration=duration,
        headless=True,
        conversation_enabled=True,
        barge_in=True,
    )


@pytest.mark.simulator
@pytest.mark.browser
@pytest.mark.live
@pytest.mark.skipif(
    sys.platform != "linux" or os.getenv("PULSE_SOURCE") != MEETING_AUDIO_MONITOR,
    reason="the meeting simulator runs in the audio test image",
)
async def test_the_twin_joins_a_simulated_meeting_and_answers_a_spoken_question(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    meeting_id = "sim-s1"
    simulator = MeetingSimulator(twin_name="Rani Twin")
    journal = journal_path(settings.paths.journal_dir, SessionId(meeting_id))
    session = asyncio.create_task(
        run_join_session(
            _request(settings, meeting_id, duration=75),
            JoinPorts(run_session=simulator.run_session, open_memory=open_persona_memory),
        )
    )

    await simulator.wait_for_lobby(session)
    simulator.join("spaces/simulated/devices/harun", "Harun")
    await simulator.admit()
    await wait_for_event(journal, "join_finished", within=30)
    await wait_for_event(journal, "conversation_bound", within=30)
    await asyncio.sleep(2)
    await simulator.say("beta-question.wav")
    decision = await wait_for_event(
        journal, "policy_decided", matching=lambda row: row.get("action") == "reply", within=30
    )
    await wait_for_event(journal, "playback_finished", within=45)
    outcome = await asyncio.wait_for(session, timeout=120)

    record = load_meeting_record(
        meeting_record_path(settings.paths.record_dir, SessionId(meeting_id))
    )
    spoken = " ".join(entry.text for entry in record if isinstance(entry, TwinSpeechEntry))
    events = {row["event"] for row in journal_rows(journal)}
    assert "orchid" in str(decision.get("heard", "")).casefold()
    assert "14 october" in spoken.casefold()
    assert outcome.session.terminal_reason == "duration_limit_reached"
    assert "turn_failed" not in events
