import asyncio
import time
from datetime import timedelta
from typing import cast

import pytest
from langchain_core.messages import HumanMessage

from fakeyou.perception import context as context_module
from fakeyou.perception.budget import PerceptionBudget
from fakeyou.perception.context import MeetingEvidence, PerceptionContext, gather_evidence
from fakeyou.perception.interpreter import PerceptionInterpreter
from fakeyou.perception.timeline import CameraFrame, MeetingTimeline
from tests._perception_fakes import START, reading, shown_timeline, slide_jpeg, vision_model
from tests._persistence_fakes import RecordingJournal

TIMELINE = ("Launch Timeline", ["Alpha: 12 January 2027", "Beta: 17 March 2027"])


def trigger(text: str, *needs: str, kind: str = "speech") -> HumanMessage:
    return HumanMessage(
        content=text,
        id="t1",
        additional_kwargs={"meeting_event": {"kind": kind, "evidence_needs": list(needs)}},
    )


def context(timeline: MeetingTimeline, *answers: str) -> PerceptionContext:
    interpreter = PerceptionInterpreter(
        vision_model(answers=answers),
        PerceptionBudget(budget_usd=1.0, max_calls=10),
        max_concurrent=1,
        snapshots=timeline.camera_frames,
    )
    return PerceptionContext(
        timeline, interpreter, max_chars=6000, clock=lambda: START + timedelta(minutes=2)
    )


def read_timeline() -> MeetingTimeline:
    timeline = shown_timeline(TIMELINE)
    timeline.read_screen("screen#1", reading("Launch Timeline", text="Beta 17 March 2027"))
    return timeline


async def test_nothing_is_looked_at_unless_the_plan_named_it() -> None:
    evidence = await gather_evidence(context(read_timeline(), "unused"), "When is beta?", ())

    assert evidence == {}


async def test_a_planned_screen_look_answers_the_spoken_question() -> None:
    evidence = await gather_evidence(
        context(read_timeline(), "Beta is 17 March 2027."), "When is beta?", ("screen",)
    )

    screen = evidence["screen"]
    assert screen["code"] == "ok"
    assert screen["answer"] == "Beta is 17 March 2027."
    assert screen["screen"] == "screen#1"
    assert screen["resolution"] == "fresh"
    assert screen["age_ms"] == 0
    assert screen["vision_latency_ms"] >= 0


async def test_a_fresh_screen_look_emits_its_resolution() -> None:
    journal = RecordingJournal()
    seeing = context(read_timeline(), "Beta is 17 March 2027.")
    evidence = MeetingEvidence(seeing, journal=journal)

    await evidence("When is beta?", ("screen",), "t1")

    [resolved] = journal.events("perception_evidence_resolved")
    assert resolved["evidence_kind"] == "screen"
    assert resolved["resolution"] == "fresh"
    assert resolved["transcript_id"] == "t1"
    assert resolved["vision_latency_ms"] >= 0


async def test_a_participant_look_among_several_people_needs_a_name() -> None:
    timeline = read_timeline()
    timeline.participant("device-budi", "Budi")
    timeline.participant("device-ayu", "Ayu")
    timeline.camera_frames.put(
        "device-budi",
        CameraFrame(slide_jpeg("Mockup"), "stream-budi", START, 0, time.monotonic()),
    )
    seeing = context(timeline, "A paper mockup.")

    named = await gather_evidence(seeing, "What is Budi wearing?", ("participant",))
    unnamed = await gather_evidence(seeing, "What is he holding?", ("participant",))

    assert named["participant"]["code"] == "ok"
    assert unnamed == {
        "participant": {
            "code": "unknown_participant",
            "participant": "",
            "resolution": "unavailable",
        }
    }


async def test_the_only_person_in_the_meeting_is_looked_at_without_a_name() -> None:
    timeline = MeetingTimeline()
    timeline.participant("device-harun", "Harun")
    timeline.camera_frames.put(
        "device-harun",
        CameraFrame(slide_jpeg("Shirt"), "stream-harun", START, 0, time.monotonic()),
    )
    seeing = context(timeline, "A dark blue shirt.")

    evidence = await gather_evidence(
        seeing, "Can you see me? What color is my shirt?", ("participant",)
    )

    assert evidence["participant"]["code"] == "ok"
    assert evidence["participant"]["answer"] == "A dark blue shirt."
    assert evidence["participant"]["strategy"] == "targeted_vision"
    assert evidence["participant"]["resolution"] == "fresh"


async def test_unknown_needs_are_ignored_and_chat_history_needs_no_model() -> None:
    evidence = await gather_evidence(
        context(read_timeline()), "What did they say?", ("chat_history", "memory", "bogus")
    )

    assert evidence == {"chat_history": {"code": "ok", "lines": []}}


class StalledInterpreter:
    async def answer(self, image: bytes, question: str, *, source: str) -> str | None:
        del image, question, source
        await asyncio.sleep(5.0)
        return "too late"


async def test_a_stalled_look_reports_failure_instead_of_holding_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context_module, "LOOK_TIMEOUT_SECONDS", 0.01)
    stalled = PerceptionContext(
        read_timeline(),
        cast(PerceptionInterpreter, StalledInterpreter()),
        max_chars=6000,
        clock=lambda: START + timedelta(minutes=2),
    )

    evidence = await gather_evidence(stalled, "When is beta?", ("screen", "chat_history"))

    assert evidence["screen"]["code"] == "look_failed"
    assert evidence["screen"]["error_type"] == "TimeoutError"
    assert evidence["chat_history"]["code"] == "ok"
