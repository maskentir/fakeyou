import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from fakeyou.conversation.turn_judge import (
    SPEAKING_NOW,
    JudgeCache,
    JudgeLine,
    JudgeWindow,
    ModelTurnJudge,
    WindowSource,
)
from fakeyou.persistence.record import MeetingRecord
from fakeyou.session.identity import Participant, PresenceFact, PresenceStatus
from tests._conversation_fakes import ScriptedJudge, ToolCallingFakeModel, judged
from tests._conversation_support import event, voiced_event


def _record(tmp_path: Path) -> MeetingRecord:
    return MeetingRecord(tmp_path / "record.jsonl")


def test_the_window_holds_speech_and_the_twin_in_order(tmp_path: Path) -> None:
    record = _record(tmp_path)
    record.add_transcript(event("t1", "Rani, when is the beta?", participant_id="ayu"))
    record.add_twin_speech("The beta is on 14 October.")
    latest = voiced_event("t2", "and the launch?", participant_id="unknown", voice_clusters=("A",))
    record.add_transcript(latest)

    window = WindowSource(record, window_lines=8).for_final(latest, twin_speaking=False)

    assert [(line.speaker, line.twin) for line in window.earlier] == [
        ("ayu", False),
        ("twin", True),
    ]
    assert window.latest == JudgeLine(speaker="voice A", text="and the launch?")
    record.close()


def test_the_window_keeps_only_the_latest_lines(tmp_path: Path) -> None:
    record = _record(tmp_path)
    for index in range(5):
        record.add_transcript(event(f"t{index}", f"line {index}"))

    window = WindowSource(record, window_lines=2).for_interim("still talking", twin_speaking=True)

    assert [line.text for line in window.earlier] == ["line 3", "line 4"]
    assert window.latest.speaker == SPEAKING_NOW
    assert window.twin_speaking is True
    record.close()


def test_a_final_reuses_the_judgement_of_its_last_interim() -> None:
    earlier = (JudgeLine("ayu", "hello"),)
    interim = JudgeWindow(earlier, JudgeLine(SPEAKING_NOW, "Rani when is the beta"))
    final = JudgeWindow(earlier, JudgeLine("ayu", "Rani, when is the beta?"))

    assert interim.key == final.key


async def test_the_cache_asks_once_for_the_same_window() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"), delay=0.01)
    cache = JudgeCache(judge)
    window = JudgeWindow((), JudgeLine("ayu", "Rani, hi"))

    assert cache.prefetch(window) is True
    assert cache.prefetch(window) is False
    first, second = await asyncio.gather(cache.judgement(window), cache.judgement(window))

    assert first.action == second.action == "reply"
    assert len(judge.windows) == 1


async def test_a_new_line_keeps_the_judgement_in_flight() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"), delay=0.05)
    cache = JudgeCache(judge)
    cache.prefetch(JudgeWindow((), JudgeLine(SPEAKING_NOW, "Rani when")))

    latest = await cache.judgement(JudgeWindow((), JudgeLine(SPEAKING_NOW, "Rani when is it")))

    assert latest.action == "reply"
    assert [window.latest.text for window in judge.windows] == ["Rani when", "Rani when is it"]


async def test_several_interims_are_judged_at_the_same_time() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"), delay=0.05)
    cache = JudgeCache(judge)
    windows = [
        JudgeWindow((), JudgeLine(SPEAKING_NOW, f"Rani when is it {index}")) for index in range(3)
    ]

    for window in windows:
        assert cache.prefetch(window) is True

    judgements = await asyncio.gather(*(cache.judgement(window) for window in windows))

    assert [judgement.action for judgement in judgements] == ["reply"] * 3
    assert len(judge.windows) == 3


async def test_the_cache_keeps_the_judgement_of_a_finished_line() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"), delay=0.0)
    cache = JudgeCache(judge)
    window = JudgeWindow((), JudgeLine(SPEAKING_NOW, "Rani when is it"))

    await cache.judgement(window)

    assert cache.prefetch(window) is False
    assert len(judge.windows) == 1


async def test_a_failed_judgement_surfaces_to_the_caller() -> None:
    cache = JudgeCache(ScriptedJudge(fail=ConnectionError("down")))

    with pytest.raises(ConnectionError):
        await cache.judgement(JudgeWindow((), JudgeLine("ayu", "Rani?")))


async def test_the_model_judge_reads_its_structured_output() -> None:
    payload = {
        "heard": "When is the beta?",
        "finished": True,
        "backchannel": False,
        "addressee": "twin",
        "action": "reply",
        "confidence": 0.8,
    }
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="", tool_calls=[{"name": "TurnJudgement", "args": payload, "id": "j"}]
            )
        ]
    )
    judge = ModelTurnJudge(model, twin_names=("Rani",), timeout_seconds=1.0)

    result = await judge.judge(
        JudgeWindow((JudgeLine("twin", "Hi.", twin=True),), JudgeLine("ayu", "Rani, beta?"))
    )

    assert result.action == "reply"
    sent = json.loads(model.seen[-1][-1].text)
    assert sent["twin_names"] == ["Rani"]
    assert sent["earlier"] == [{"speaker": "twin", "text": "Hi.", "by_twin": True}]
    assert sent["latest"] == {"speaker": "ayu", "text": "Rani, beta?"}


def claimed(*names: str, status: PresenceStatus = "complete") -> PresenceFact:
    return PresenceFact(
        status=status,
        confirmed_min=len(names),
        plausible_max=len(names) if status == "complete" else None,
        unresolved=0,
        shared_endpoints=0,
        drift=False,
        twin_identified=True,
        participants=tuple(
            Participant(
                f"dev-{name}", name, host=False, voices=(), enrolled=(), claim_conflict=False
            )
            for name in names
        ),
    )


async def test_the_judge_payload_carries_presence_certainty() -> None:
    payload: dict[str, object] = {
        "heard": "can you see my screen?",
        "finished": True,
        "backchannel": False,
        "addressee": "twin",
        "action": "reply",
        "confidence": 0.9,
    }

    def respond() -> AIMessage:
        return AIMessage(
            content="", tool_calls=[{"name": "TurnJudgement", "args": payload, "id": "j"}]
        )

    model = ToolCallingFakeModel(responses=[respond(), respond()], seen=[])
    judge = ModelTurnJudge(model, twin_names=("Rani",), timeout_seconds=1.0)

    await judge.judge(
        JudgeWindow(
            earlier=(JudgeLine(speaker="ayu", text="morning"),),
            latest=JudgeLine(speaker="ayu", text="can you see my screen?"),
            presence=claimed("ayu"),
        )
    )
    sent = json.loads(model.seen[-1][-1].text)
    assert sent["human_speakers"] == ["ayu"]
    assert sent["participants"] == [{"ref": "p1", "shown_as": "ayu", "name_is": "claimed"}]
    assert sent["presence"] == {
        "status": "complete",
        "confirmed_min": 1,
        "plausible_max": 1,
        "unresolved": 0,
    }
    assert sent["sole_human_responder"] is True

    await judge.judge(
        JudgeWindow(
            earlier=(JudgeLine(speaker="ayu", text="morning"),),
            latest=JudgeLine(speaker="budi", text="can everyone see my screen?"),
            presence=claimed("ayu", status="incomplete"),
        )
    )
    sent = json.loads(model.seen[-1][-1].text)
    assert sent["human_speakers"] == ["ayu", "budi"]
    assert sent["presence"]["plausible_max"] is None
    assert sent["sole_human_responder"] is False
