import asyncio
import hashlib
import json
import os
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from evals._meeting_simulator import (
    FIXTURES,
    FailureCategory,
    MeetingSimulator,
    ScenarioFailure,
    journal_rows,
    require,
    running_session,
    session_modes,
    wait_for_event,
)
from evals._provider_doubles import SNAPSHOT, ProviderDoubles, provider_doubles
from evals._replay_cassette import CassetteError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import PathSettings, Settings
from fakeyou.memory.persona import OperatorIdentity
from fakeyou.observability.journal import journal_path
from fakeyou.orchestration.join import run_join_session
from fakeyou.orchestration.join_types import JoinPorts, JoinRequest
from fakeyou.orchestration.providers import open_persona_memory
from fakeyou.persistence.owner_model import FrozenOwnerModel
from fakeyou.persistence.record import TwinSpeechEntry, load_meeting_record, meeting_record_path

MEETING_AUDIO_MONITOR = "meeting_audio.monitor"
LINK = "https://meet.google.com/sim-ulat-edm"
HARUN = "spaces/simulated/devices/harun"
BIMA = "spaces/simulated/devices/bima"
RED = "#c62828"
MAX_REPLY_LATENCY_SECONDS = 8.0
MAX_STOP_LATENCY_SECONDS = 4.0
INTERRUPT_AFTER_SECONDS = 1.0
REPORTED_EVENTS = frozenset(
    {
        "conversation_bound",
        "session_mode_transitioned",
        "perception_streams_bound",
        "transcript_finalized",
        "policy_decided",
        "plan_selected",
        "meet_action_finished",
        "chat_sent",
        "playback_started",
        "playback_finished",
        "playback_dropped",
        "barge_in_detected",
        "turn_failed",
        "fault_recorded",
        "model_input_recorded",
    }
)


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


PLAN_FACTS = (
    {"id": "plan-freeze", "text": "Step one: Dimas freezes the Orchid code on 30 September 2026."},
    {"id": "plan-beta", "text": "Step two: Rani opens the Orchid beta on 14 October 2026."},
    {"id": "plan-docs", "text": "Step three: Ayu publishes the Orchid guides on 21 October 2026."},
    {"id": "plan-launch", "text": "Step four: the Orchid public launch is on 4 November 2026."},
)


def _brief(directory: Path, meeting_id: str, *, plan: bool = False) -> Path:
    brief = {
        "schema_version": "meeting_brief/v2",
        "owner_id": "sim_owner",
        "twin_id": "sim_twin",
        "meeting_id": meeting_id,
        "agenda": ["Orchid weekly sync"],
        "approved_facts": [
            {"id": "beta-date", "text": "The Orchid beta launch is planned for 14 October 2026."},
            {"id": "checklist-owner", "text": "Rani owns the Orchid launch checklist."},
            *(PLAN_FACTS if plan else ()),
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
        owner_model=FrozenOwnerModel.model_validate_json(
            (FIXTURES / "owner-model.json").read_bytes()
        ),
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


def _perception_ready(rows: list[dict[str, Any]]) -> bool:
    events = [row.get("event") for row in rows]
    return (
        "conversation_bound" in events
        and "perception_streams_bound" in events
        and "perception_degraded" not in session_modes(rows)
    )


async def _admitted(simulator: MeetingSimulator, session: asyncio.Task[Any], journal: Path) -> None:
    await simulator.wait_for_lobby(session)
    await simulator.join(HARUN, "Harun")
    await simulator.admit()
    await wait_for_event(journal, "conversation_bound", within=30, category="protocol_failure")
    await wait_for_event(
        journal,
        "session_mode_transitioned",
        matching=lambda _row: _perception_ready(journal_rows(journal)),
        within=30,
        category="environment_failure",
    )


@dataclass(slots=True)
class Meeting:
    meeting_id: str
    settings: Settings
    simulator: MeetingSimulator
    journal: Path
    session: asyncio.Task[Any]
    doubles: ProviderDoubles

    def promote(self) -> None:
        self.doubles.promote()

    def twin_speech(self) -> list[TwinSpeechEntry]:
        path = meeting_record_path(self.settings.paths.record_dir, SessionId(self.meeting_id))
        if not path.exists():
            return []
        return [entry for entry in load_meeting_record(path) if isinstance(entry, TwinSpeechEntry)]

    def events(self) -> set[str]:
        return {row["event"] for row in journal_rows(self.journal)}

    def keep(self, name: str, data: bytes) -> None:
        folder = self.settings.paths.record_dir / self.meeting_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)

    async def next_turn(self, after: int, within: float = 60.0) -> str:
        spoken: list[TwinSpeechEntry] = []
        try:
            async with asyncio.timeout(within):
                while not spoken:
                    spoken = [entry for entry in self.twin_speech()[after:] if entry.text]
                    if not spoken:
                        await asyncio.sleep(0.1)
        except TimeoutError as error:
            raise ScenarioFailure(
                "behavioral_assertion_failure", f"the twin said nothing new within {within} s"
            ) from error
        started = spoken[0].at
        await wait_for_event(
            self.journal,
            "conversation_transitioned",
            matching=lambda row: (
                row.get("to_phase") == "listening" and datetime.fromisoformat(row["at"]) > started
            ),
            within=within,
        )
        return " ".join(entry.text for entry in self.twin_speech()[after:]).casefold()

    async def ask(self, clip: str) -> str:
        before = len(self.twin_speech())
        await self.simulator.say(clip, speaker=HARUN)
        return await self.next_turn(before)


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _category(
    error: BaseException | None, replay: Mapping[str, Any] | None
) -> FailureCategory | None:
    if replay is not None and replay.get("misses"):
        return "provider_replay_mismatch"
    if error is None:
        return None
    if isinstance(error, ScenarioFailure):
        return error.category
    if isinstance(error, CassetteError):
        return "provider_replay_mismatch"
    if isinstance(error, AssertionError):
        return "behavioral_assertion_failure"
    return "environment_failure"


def _report(
    meeting_id: str,
    doubles: ProviderDoubles,
    journal: Path,
    actions: list[dict[str, Any]],
    error: BaseException | None,
) -> dict[str, Any]:
    provider = doubles.report()
    found = provider.get("replay")
    replay: Mapping[str, Any] | None = found if isinstance(found, Mapping) else None
    rows = journal_rows(journal)
    return {
        "scenario": meeting_id,
        "revision": os.environ.get("SIMULATOR_REVISION", "unknown"),
        "fixtures": {
            "cassette_sha256": provider.get("cassette_sha256"),
            "settings_sha256": _digest(SNAPSHOT),
            "clips": {
                action["clip"]: _digest(FIXTURES / action["clip"])
                for action in actions
                if "clip" in action
            },
        },
        "outcome": "passed"
        if error is None and not (replay and replay.get("misses"))
        else "failed",
        "category": _category(error, replay),
        "failure": None if error is None else f"{type(error).__name__}: {error}"[:2000],
        "simulator_actions": actions,
        "timeline": [
            {
                key: row.get(key)
                for key in (
                    "at",
                    "event",
                    "action",
                    "reason",
                    "to_phase",
                    "entered",
                    "left",
                    "slot",
                    "judge_purpose",
                    "playback_outcome",
                    "heard",
                )
            }
            for row in rows
            if row.get("event") in REPORTED_EVENTS
        ],
        "provider": provider,
    }


@asynccontextmanager
async def _meeting(
    tmp_path: Path,
    meeting_id: str,
    *,
    duration: float = 300,
    brief: bool = True,
    plan: bool = False,
) -> AsyncIterator[Meeting]:
    doubles = provider_doubles(meeting_id)
    actions: list[dict[str, Any]] = []
    settings = _settings(tmp_path)
    journal = journal_path(settings.paths.journal_dir, SessionId(meeting_id))
    error: BaseException | None = None

    def said(clip: str) -> None:
        actions.append({"at": time.time(), "action": "say", "clip": clip})
        doubles.script(clip)

    try:
        async with doubles.running():
            settings = doubles.settings(settings)
            simulator = MeetingSimulator(twin_name="Rani Twin", on_say=said)
            briefing = _brief(tmp_path, meeting_id, plan=plan) if brief else None
            request = _request(settings, meeting_id, duration, briefing)
            async with running_session(run_join_session(request, _ports(simulator))) as session:
                await _admitted(simulator, session, journal)
                yield Meeting(meeting_id, settings, simulator, journal, session, doubles)
        failures = doubles.integrity_failures()
        require(not failures, "provider_replay_mismatch", "; ".join(failures))
    except BaseException as raised:
        error = raised
        raise
    finally:
        report = _report(meeting_id, doubles, journal, actions, error)
        folder = settings.paths.record_dir.parent / "reports"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{meeting_id}.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )


async def test_the_twin_joins_a_simulated_meeting_and_answers_a_spoken_question(
    tmp_path: Path,
) -> None:
    async with _meeting(tmp_path, "sim-s1", duration=75, brief=False) as meeting:
        await meeting.simulator.say("owner-beta-question.wav", speaker=HARUN)
        decision = await wait_for_event(
            meeting.journal,
            "policy_decided",
            matching=lambda row: row.get("action") == "reply",
            within=30,
            category="policy_failure",
        )
        await wait_for_event(
            meeting.journal, "playback_finished", within=45, category="audio_failure"
        )
        outcome = await asyncio.wait_for(meeting.session, timeout=120)

    spoken = " ".join(entry.text for entry in meeting.twin_speech())
    require(
        "orchid" in str(decision.get("heard", "")).casefold(),
        "behavioral_assertion_failure",
        f"heard {decision.get('heard')!r}",
    )
    require("14 october" in spoken.casefold(), "behavioral_assertion_failure", f"spoke {spoken!r}")
    require(
        "reply_draft_accepted" in meeting.events(),
        "behavioral_assertion_failure",
        "no accepted reply draft",
    )
    require(
        outcome.session.terminal_reason == "duration_limit_reached",
        "behavioral_assertion_failure",
        f"ended with {outcome.session.terminal_reason}",
    )
    require("turn_failed" not in meeting.events(), "behavioral_assertion_failure", "a turn failed")
    meeting.promote()


async def test_the_twin_answers_chat_and_acts_on_the_meeting_when_asked(tmp_path: Path) -> None:
    async with _meeting(tmp_path, "sim-s2") as meeting:
        simulator = meeting.simulator
        await simulator.chat(HARUN, "Rani Twin, who owns the Orchid launch checklist?")
        answered = await simulator.wait_for_state(
            lambda state: any("rani" in line.casefold() for line in state["sent"]),
            expecting="a chat answer naming Rani",
        )
        await simulator.say("raise-hand.wav", speaker=HARUN)
        raised = await simulator.wait_for_state(
            lambda state: str(state["hand"]).startswith("Lower hand"),
            expecting="a raised hand",
            within=45,
        )
        await simulator.say("lower-hand.wav", speaker=HARUN)
        lowered = await simulator.wait_for_state(
            lambda state: str(state["hand"]).startswith("Raise hand"),
            expecting="a lowered hand",
            within=45,
        )
        await simulator.say("send-date-in-chat.wav", speaker=HARUN)
        dated = await simulator.wait_for_state(
            lambda state: any("14 october" in line.casefold() for line in state["sent"]),
            expecting="the beta date in chat",
        )
        await simulator.say("leave.wav", speaker=HARUN)
        outcome = await asyncio.wait_for(meeting.session, timeout=90)

    require(answered["sent"], "behavioral_assertion_failure", "no chat answer")
    require(raised["hand"].startswith("Lower hand"), "behavioral_assertion_failure", str(raised))
    require(lowered["hand"].startswith("Raise hand"), "behavioral_assertion_failure", str(lowered))
    require(
        any("14 october" in line.casefold() for line in dated["sent"]),
        "behavioral_assertion_failure",
        str(dated),
    )
    require(meeting.simulator.left, "behavioral_assertion_failure", "the twin did not leave")
    require(
        outcome.session.phase == "ended",
        "behavioral_assertion_failure",
        f"phase {outcome.session.phase}",
    )
    require("turn_failed" not in meeting.events(), "behavioral_assertion_failure", "a turn failed")
    meeting.promote()


SLIDE_ONLY_TOKEN = "HARBOR-47"


def _screen_question(rows: list[dict[str, Any]], after: int) -> dict[str, Any]:
    decided = [
        row for row in rows[after:] if row["event"] == "policy_decided" and "token" in row["heard"]
    ]
    require(
        bool(decided), "behavioral_assertion_failure", "the review-token question was not judged"
    )
    return decided[-1]


def _screen_looks(rows: list[dict[str, Any]], transcript_id: str) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row["event"] == "perception_evidence_resolved"
        and row.get("transcript_id") == transcript_id
        and row.get("evidence_kind") == "screen"
        and row.get("resolution") != "unavailable"
    ]


async def test_the_twin_corrects_a_wrong_slide_and_reads_the_shared_screen(tmp_path: Path) -> None:
    async with _meeting(tmp_path, "sim-s3-screen") as meeting:
        await meeting.simulator.share(
            HARUN,
            "Orchid Launch Plan",
            [
                "Beta launch: 21 October 2026",
                f"Review token: {SLIDE_ONLY_TOKEN}",
                "Checklist: Rani",
            ],
        )
        await wait_for_event(meeting.journal, "screen_reading_finished", within=60)
        correction = await meeting.next_turn(0, within=90)
        asked_at = len(journal_rows(meeting.journal))
        read = await meeting.ask("review-token.wav")

    rows = journal_rows(meeting.journal)
    question = _screen_question(rows, asked_at)
    require(
        "14 october" in correction, "behavioral_assertion_failure", f"correction {correction!r}"
    )
    require(
        question.get("state_question") == "none" and "screen" in question["evidence_needs"],
        "behavioral_assertion_failure",
        f"the review-token question was not judged a screen look: {question}",
    )
    require(
        bool(_screen_looks(rows, question["transcript_id"])),
        "behavioral_assertion_failure",
        "the review token was answered without inspecting the shared screen",
    )
    require(
        SLIDE_ONLY_TOKEN.casefold().replace("-", " ") in read.replace("-", " "),
        "behavioral_assertion_failure",
        f"read {read!r}",
    )
    require("turn_failed" not in meeting.events(), "behavioral_assertion_failure", "a turn failed")
    meeting.promote()


def _camera_of(device: str) -> Callable[[Mapping[str, Any]], bool]:
    def observed(row: Mapping[str, Any]) -> bool:
        streams = row.get("output_streams") or []
        return int(row.get("camera_frames") or 0) > 0 and any(
            str(stream).startswith(f"{device}:video:") for stream in streams
        )

    return observed


async def test_the_twin_only_describes_a_camera_it_can_attribute(tmp_path: Path) -> None:
    async with _meeting(tmp_path, "sim-s3-camera", brief=False) as meeting:
        await meeting.simulator.join(BIMA, "Bima")
        await meeting.simulator.camera(HARUN, RED)
        await wait_for_event(
            meeting.journal,
            "perception_batch_observed",
            matching=_camera_of(HARUN),
            within=30,
            category="environment_failure",
        )
        unattributed = await meeting.ask("shirt-color.wav")
        named = await meeting.ask("harun-shirt-color.wav")

    require(
        "red" not in unattributed,
        "policy_failure",
        f"described an unattributed camera: {unattributed!r}",
    )
    require("red" in named, "behavioral_assertion_failure", f"named answer {named!r}")
    require("turn_failed" not in meeting.events(), "behavioral_assertion_failure", "a turn failed")
    meeting.promote()


async def test_the_twin_sends_the_reaction_meet_offers_for_what_was_asked(tmp_path: Path) -> None:
    async with _meeting(tmp_path, "sim-reactions") as meeting:
        simulator = meeting.simulator
        await simulator.say("react-thumbs-up.wav", speaker=HARUN)
        thumbs = await simulator.wait_for_state(
            lambda state: "\N{THUMBS UP SIGN}" in state["reactions"],
            expecting="a thumbs up",
            within=45,
        )
        await wait_for_event(meeting.journal, "meet_action_finished", within=30)
        await simulator.say("send-heart.wav", speaker=HARUN)
        heart = await simulator.wait_for_state(
            lambda state: "\N{SPARKLING HEART}" in state["reactions"],
            expecting="a heart",
            within=45,
        )

    require(
        thumbs["reactions"][0] == "\N{THUMBS UP SIGN}", "behavioral_assertion_failure", str(thumbs)
    )
    require(
        heart["reactions"] == ["\N{THUMBS UP SIGN}", "\N{SPARKLING HEART}"],
        "behavioral_assertion_failure",
        str(heart),
    )
    require("turn_failed" not in meeting.events(), "behavioral_assertion_failure", "a turn failed")
    meeting.promote()


async def test_the_meeting_hears_the_twin_answer_quickly_and_stop_when_cut_off(
    tmp_path: Path,
) -> None:
    async with _meeting(tmp_path, "sim-s4-timing", plan=True) as meeting:
        simulator = meeting.simulator
        question = await simulator.say("beta-question.wav", speaker=HARUN)
        answer = await simulator.wait_for_twin_voice(
            lambda span: span.start > question.end, expecting="an answer after the question"
        )
        await meeting.next_turn(0)
        before = len(meeting.twin_speech())
        request_plan = await simulator.say("walk-through-plan.wav", speaker=HARUN)
        await simulator.wait_for_twin_voice(
            lambda span: (
                span.start > request_plan.end and span.end - span.start >= INTERRUPT_AFTER_SECONDS
            ),
            expecting=f"{INTERRUPT_AFTER_SECONDS} s of the plan",
        )
        cut = await simulator.say("hold-on.wav", speaker=HARUN)
        await wait_for_event(
            meeting.journal,
            "playback_dropped",
            matching=lambda row: row.get("playback_outcome") == "interrupted_after_audio",
            within=15,
            category="audio_failure",
        )
        voice = await simulator.wait_for_twin_silence(cut.start)
        meeting.keep("twin.webm", await simulator.twin_recording())

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
    meeting.keep("timing.json", json.dumps(timing, indent=2).encode())
    interrupted = [entry for entry in meeting.twin_speech()[before:] if entry.interrupted]
    require(reply_latency < MAX_REPLY_LATENCY_SECONDS, "timing_failure", json.dumps(timing))
    require(stop_latency < MAX_STOP_LATENCY_SECONDS, "timing_failure", json.dumps(timing))
    require(interrupted, "behavioral_assertion_failure", "no interrupted twin speech was recorded")
    meeting.promote()
