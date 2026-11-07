import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from evals._meeting_simulator import (
    FIXTURES,
    MeetingSimulator,
    journal_rows,
    running_session,
    wait_for_event,
)
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
HARUN = "spaces/simulated/devices/harun"
BIMA = "spaces/simulated/devices/bima"
RED = "#c62828"
MAX_REPLY_LATENCY_SECONDS = 8.0
MAX_STOP_LATENCY_SECONDS = 4.0


def _settings(tmp_path: Path) -> Settings:
    settings = Settings()
    artifacts = os.environ.get("SIMULATOR_ARTIFACT_DIR")
    base = Path(artifacts) if artifacts else tmp_path
    paths = PathSettings(
        journal_dir=base / "journals",
        record_dir=base / "meetings",
        browser_profile_dir=tmp_path / "profiles",
        browser_output_dir=tmp_path / "browser",
    )
    return settings.model_copy(update={"paths": paths})


def _brief(directory: Path, meeting_id: str) -> Path:
    brief = {
        "schema_version": "meeting_brief/v2",
        "owner_id": "sim_owner",
        "twin_id": "sim_twin",
        "meeting_id": meeting_id,
        "agenda": ["Orchid weekly sync"],
        "approved_facts": [
            {"id": "beta-date", "text": "The Orchid beta launch is planned for 14 October 2026."},
            {"id": "checklist-owner", "text": "Rani owns the Orchid launch checklist."},
        ],
        "permissions": ["explain_facts", "meet_act", "meet_chat", "meet_leave"],
        "knowledge_scope": "brief_only",
    }
    path = directory / f"{meeting_id}-brief.json"
    path.write_text(json.dumps(brief), encoding="utf-8")
    return path


def _request(
    settings: Settings, meeting_id: str, duration: float, brief: Path | None = None
) -> JoinRequest:
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
        brief_path=brief,
    )


pytestmark = [
    pytest.mark.simulator,
    pytest.mark.browser,
    pytest.mark.live,
    pytest.mark.skipif(
        sys.platform != "linux" or os.getenv("PULSE_SOURCE") != MEETING_AUDIO_MONITOR,
        reason="the meeting simulator runs in the audio test image",
    ),
]


def _ports(simulator: MeetingSimulator) -> JoinPorts:
    return JoinPorts(run_session=simulator.run_session, open_memory=open_persona_memory)


def _twin_speech(settings: Settings, meeting_id: str) -> list[TwinSpeechEntry]:
    path = meeting_record_path(settings.paths.record_dir, SessionId(meeting_id))
    if not path.exists():
        return []
    return [entry for entry in load_meeting_record(path) if isinstance(entry, TwinSpeechEntry)]


async def _next_turn(
    settings: Settings, meeting_id: str, journal: Path, after: int, within: float = 60.0
) -> str:
    async with asyncio.timeout(within):
        while True:
            spoken = [entry for entry in _twin_speech(settings, meeting_id)[after:] if entry.text]
            if spoken:
                break
            await asyncio.sleep(0.25)
    started = spoken[0].at
    await wait_for_event(
        journal,
        "conversation_transitioned",
        matching=lambda row: (
            row.get("to_phase") == "listening" and datetime.fromisoformat(row["at"]) > started
        ),
        within=within,
    )
    heard = _twin_speech(settings, meeting_id)[after:]
    return " ".join(entry.text for entry in heard).casefold()


async def _ask(
    simulator: MeetingSimulator, settings: Settings, meeting_id: str, journal: Path, clip: str
) -> str:
    before = len(_twin_speech(settings, meeting_id))
    await simulator.say(clip, speaker=HARUN)
    return await _next_turn(settings, meeting_id, journal, before)


async def _admitted(simulator: MeetingSimulator, session: asyncio.Task[Any], journal: Path) -> None:
    await simulator.wait_for_lobby(session)
    await simulator.join(HARUN, "Harun")
    await simulator.admit()
    await wait_for_event(journal, "conversation_bound", within=30)
    await asyncio.sleep(2)


async def test_the_twin_joins_a_simulated_meeting_and_answers_a_spoken_question(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    meeting_id = "sim-s1"
    simulator = MeetingSimulator(twin_name="Rani Twin")
    journal = journal_path(settings.paths.journal_dir, SessionId(meeting_id))

    async with running_session(
        run_join_session(_request(settings, meeting_id, duration=75), _ports(simulator))
    ) as session:
        await _admitted(simulator, session, journal)
        await simulator.say("beta-question.wav", speaker=HARUN)
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


async def test_the_twin_answers_chat_and_acts_on_the_meeting_when_asked(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    meeting_id = "sim-s2"
    simulator = MeetingSimulator(twin_name="Rani Twin")
    journal = journal_path(settings.paths.journal_dir, SessionId(meeting_id))
    request = _request(settings, meeting_id, duration=300, brief=_brief(tmp_path, meeting_id))

    async with running_session(run_join_session(request, _ports(simulator))) as session:
        await _admitted(simulator, session, journal)
        await simulator.chat(HARUN, "Rani, who owns the Orchid launch checklist?")
        answered = await simulator.wait_for_state(
            lambda state: any("rani" in line.casefold() for line in state["sent"]), within=60
        )
        await simulator.say("raise-hand.wav", speaker=HARUN)
        raised = await simulator.wait_for_state(
            lambda state: str(state["hand"]).startswith("Lower hand"), within=45
        )
        await simulator.say("lower-hand.wav", speaker=HARUN)
        lowered = await simulator.wait_for_state(
            lambda state: str(state["hand"]).startswith("Raise hand"), within=45
        )
        await simulator.say("send-date-in-chat.wav", speaker=HARUN)
        dated = await simulator.wait_for_state(
            lambda state: any("14 october" in line.casefold() for line in state["sent"]),
            within=60,
        )
        await simulator.say("leave.wav", speaker=HARUN)
        outcome = await asyncio.wait_for(session, timeout=90)

    assert answered["sent"]
    assert raised["hand"].startswith("Lower hand")
    assert lowered["hand"].startswith("Raise hand")
    assert any("14 october" in line.casefold() for line in dated["sent"])
    assert simulator.left
    assert outcome.session.phase == "ended"
    assert "turn_failed" not in {row["event"] for row in journal_rows(journal)}


async def test_the_twin_corrects_a_wrong_slide_and_reads_the_shared_screen(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    meeting_id = "sim-s3-screen"
    simulator = MeetingSimulator(twin_name="Rani Twin")
    journal = journal_path(settings.paths.journal_dir, SessionId(meeting_id))
    request = _request(settings, meeting_id, duration=300, brief=_brief(tmp_path, meeting_id))

    async with running_session(run_join_session(request, _ports(simulator))) as session:
        await _admitted(simulator, session, journal)
        await simulator.share(
            HARUN, "Orchid Launch Plan", ["Beta launch: 21 October 2026", "Checklist: Rani"]
        )
        await wait_for_event(journal, "screen_reading_finished", within=60)
        correction = await _next_turn(settings, meeting_id, journal, 0, within=90)
        described = await _ask(simulator, settings, meeting_id, journal, "whats-on-screen.wav")

    assert "14 october" in correction
    assert "orchid" in described or "launch" in described
    assert "turn_failed" not in {row["event"] for row in journal_rows(journal)}


async def test_the_twin_only_describes_a_camera_it_can_attribute(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    meeting_id = "sim-s3-camera"
    simulator = MeetingSimulator(twin_name="Rani Twin")
    journal = journal_path(settings.paths.journal_dir, SessionId(meeting_id))
    request = _request(settings, meeting_id, duration=300)

    async with running_session(run_join_session(request, _ports(simulator))) as session:
        await _admitted(simulator, session, journal)
        await simulator.join(BIMA, "Bima")
        await simulator.camera(HARUN, RED)
        await asyncio.sleep(5)
        unattributed = await _ask(simulator, settings, meeting_id, journal, "shirt-color.wav")
        named = await _ask(simulator, settings, meeting_id, journal, "harun-shirt-color.wav")

    assert "red" not in unattributed
    assert "red" in named
    assert "turn_failed" not in {row["event"] for row in journal_rows(journal)}


async def test_the_twin_sends_the_reaction_meet_offers_for_what_was_asked(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    meeting_id = "sim-reactions"
    simulator = MeetingSimulator(twin_name="Rani Twin")
    journal = journal_path(settings.paths.journal_dir, SessionId(meeting_id))
    request = _request(settings, meeting_id, duration=300, brief=_brief(tmp_path, meeting_id))

    async with running_session(run_join_session(request, _ports(simulator))) as session:
        await _admitted(simulator, session, journal)
        await simulator.say("react-thumbs-up.wav", speaker=HARUN)
        thumbs = await simulator.wait_for_state(
            lambda state: "\N{THUMBS UP SIGN}" in state["reactions"], within=45
        )
        await wait_for_event(journal, "meet_action_finished", within=30)
        await simulator.say("send-heart.wav", speaker=HARUN)
        heart = await simulator.wait_for_state(
            lambda state: "\N{SPARKLING HEART}" in state["reactions"], within=45
        )

    assert thumbs["reactions"][0] == "\N{THUMBS UP SIGN}"
    assert heart["reactions"] == ["\N{THUMBS UP SIGN}", "\N{SPARKLING HEART}"]
    assert "turn_failed" not in {row["event"] for row in journal_rows(journal)}


def _keep(settings: Settings, meeting_id: str, name: str, data: bytes) -> None:
    folder = settings.paths.record_dir / meeting_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)


async def test_the_meeting_hears_the_twin_answer_quickly_and_stop_when_cut_off(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    meeting_id = "sim-s4-timing"
    simulator = MeetingSimulator(twin_name="Rani Twin")
    journal = journal_path(settings.paths.journal_dir, SessionId(meeting_id))
    request = _request(settings, meeting_id, duration=300, brief=_brief(tmp_path, meeting_id))

    async with running_session(run_join_session(request, _ports(simulator))) as session:
        await _admitted(simulator, session, journal)
        question = await simulator.say("beta-question.wav", speaker=HARUN)
        answer = await simulator.wait_for_twin_voice(lambda span: span.start > question.end)
        await _next_turn(settings, meeting_id, journal, 0)
        before = len(_twin_speech(settings, meeting_id))
        request_plan = await simulator.say("walk-through-plan.wav", speaker=HARUN)
        await simulator.wait_for_twin_voice(lambda span: span.start > request_plan.end)
        await asyncio.sleep(1.0)
        cut = await simulator.say("hold-on.wav", speaker=HARUN)
        await asyncio.sleep(3.0)
        voice = await simulator.twin_voice()
        _keep(settings, meeting_id, "twin.webm", await simulator.twin_recording())

    heard_over = [span.end for span in voice if span.start < cut.end and span.end > cut.start]
    reply_latency = answer.start - question.end
    stop_latency = max(heard_over, default=cut.start) - cut.start
    timing = {
        "reply_latency": reply_latency,
        "stop_latency": stop_latency,
        "question": asdict(question),
        "cut": asdict(cut),
        "twin_voice": [asdict(span) for span in voice],
    }
    _keep(settings, meeting_id, "timing.json", json.dumps(timing, indent=2).encode())
    later = _twin_speech(settings, meeting_id)[before:]
    interrupted = [entry for entry in later if entry.interrupted]
    assert reply_latency < MAX_REPLY_LATENCY_SECONDS
    assert stop_latency < MAX_STOP_LATENCY_SECONDS
    assert interrupted
