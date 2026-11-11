import asyncio
from datetime import timedelta
from typing import cast

import pytest
from langchain_core.messages import HumanMessage

from fakeyou.perception import context as context_module
from fakeyou.perception.budget import PerceptionBudget
from fakeyou.perception.camera_evidence import CameraObservation
from fakeyou.perception.context import PerceptionContext, gather_evidence
from fakeyou.perception.interpreter import PerceptionInterpreter
from fakeyou.perception.timeline import MeetingTimeline
from tests._perception_fakes import START, reading, shown_timeline, slide_jpeg, vision_model

TIMELINE = ("Launch Timeline", ["Alpha: 12 January 2027", "Beta: 17 March 2027"])


def trigger(text: str, *needs: str, kind: str = "speech") -> HumanMessage:
    return HumanMessage(
        content=text,
        id="t1",
        additional_kwargs={"meeting_event": {"kind": kind, "evidence_needs": list(needs)}},
    )


def context(timeline: MeetingTimeline, *answers: str) -> PerceptionContext:
    interpreter = PerceptionInterpreter(
        vision_model(
            answers=answers,
            observations=(
                CameraObservation(
                    participant_id="person#1",
                    observed_at=START,
                    clothing="light ivory shirt",
                    visible_objects=[],
                    background="indoor room",
                    appearance="a person speaking",
                ),
            ),
        ),
        PerceptionBudget(budget_usd=1.0, max_calls=10),
        max_concurrent=1,
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

    assert evidence == {
        "screen": {"code": "ok", "answer": "Beta is 17 March 2027.", "screen": "screen#1"}
    }


async def test_a_participant_look_among_several_people_needs_a_name() -> None:
    timeline = read_timeline()
    timeline.participant("device-budi", "Budi")
    timeline.participant("device-ayu", "Ayu")
    timeline.camera_frames.put("device-budi", slide_jpeg("Mockup"))
    seeing = context(timeline, "A paper mockup.")

    named = await gather_evidence(seeing, "What is Budi holding?", ("participant",))
    unnamed = await gather_evidence(seeing, "What is he holding?", ("participant",))

    assert named["participant"]["code"] == "ok"
    assert unnamed == {"participant": {"code": "unknown_participant", "participant": ""}}


async def test_the_only_person_in_the_meeting_is_looked_at_without_a_name() -> None:
    timeline = MeetingTimeline()
    timeline.participant("device-harun", "Harun")
    timeline.camera_frames.put("device-harun", slide_jpeg("Shirt"))
    seeing = context(timeline, "A dark blue shirt.")

    evidence = await gather_evidence(
        seeing, "Can you see me? What color is my shirt?", ("participant",)
    )

    assert evidence["participant"]["code"] == "ok"
    assert "light ivory shirt" in evidence["participant"]["answer"]
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
