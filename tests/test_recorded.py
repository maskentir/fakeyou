import asyncio
import time
import warnings
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.reply_planner import ReplyPlanner
from fakeyou.conversation.turn_judge import JudgeLine, JudgeWindow
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.persistence.record import TranscriptEntry
from fakeyou.replay.fixture import (
    JudgeVerdict,
    RecordedPlan,
    ReplySentence,
    ReplyTiming,
    SpokenFinal,
    SynthesisFault,
    SynthesisTiming,
    load_fixture,
)
from fakeyou.replay.pacer import BYTES_PER_SECOND, create_pacer_backend
from fakeyou.replay.recorded import RecordedJudge, RecordedModel, RecordedSynthesizer
from fakeyou.replay.virtual_time import VirtualTimeLoop

CHUNKS = ((b"\x00" * 4800, 0.1), (b"\x00" * 4800, 0.1))


def run_virtual[T](main: Callable[[], Coroutine[Any, Any, T]]) -> T:
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
            return runner.run(main())


async def drain(stream: AsyncIterator[bytes]) -> list[tuple[float, int]]:
    loop = asyncio.get_running_loop()
    marks: list[tuple[float, int]] = []
    async for chunk in stream:
        marks.append((loop.time(), len(chunk)))
    return marks


def test_the_synthesizer_holds_the_first_chunk_for_the_recorded_delay() -> None:
    async def main() -> list[tuple[float, int]]:
        return await drain(
            RecordedSynthesizer({("t1", 0): SynthesisTiming(300.0, 900.0, 9600, 26)}).stream(
                "hello", sentence_index=0, transcript_id="t1"
            )
        )

    marks = run_virtual(main)

    assert marks[0][0] == pytest.approx(0.3)
    assert sum(size for _, size in marks) == 9600
    assert marks[-1][0] == pytest.approx(0.9)


def test_an_unrecorded_synthesis_is_sized_by_the_recorded_speech_rate() -> None:
    timings = {
        ("t1", 0): SynthesisTiming(200.0, 800.0, 4800, 10),
        ("t2", 0): SynthesisTiming(400.0, 1000.0, 4800, 10),
    }

    async def main() -> list[tuple[float, int]]:
        return await drain(
            RecordedSynthesizer(timings).stream("hello", sentence_index=0, transcript_id="missing")
        )

    marks = run_virtual(main)

    assert marks[0][0] == pytest.approx(0.3)
    assert sum(size for _, size in marks) == 2400


def test_a_recorded_provider_failure_fails_again() -> None:
    faults = {("t1", 0): (SynthesisFault(350.0, "unavailable"),)}

    async def main() -> float:
        loop = asyncio.get_running_loop()
        stream = RecordedSynthesizer({}, faults=faults).stream(
            "hello", sentence_index=0, transcript_id="t1"
        )
        with pytest.raises(ProviderFault):
            await drain(stream)
        return loop.time()

    assert run_virtual(main) == pytest.approx(0.35)


def reply_verdict() -> JudgeVerdict:
    return JudgeVerdict(
        400.0,
        {
            "reason": "judge_reply",
            "action": "reply",
            "confidence": 0.9,
            "heard": "can you see my screen",
            "addressee": "twin",
            "finished": True,
            "backchannel": False,
            "requests_chat": False,
            "meet_action": "none",
            "action_only": False,
            "logistics_check": False,
            "state_question": "none",
            "reaction_emoji": None,
            "evidence_needs": ["screen"],
        },
    )


def spoken(transcript_id: str, text: str, offset: float) -> SpokenFinal:
    entry = TranscriptEntry(
        at=datetime(2026, 10, 27, tzinfo=UTC),
        seq=1,
        id=transcript_id,
        spoken_at="2026-10-27T00:00:00+00:00",
        source="meeting_audio",
        text=text,
        participant_id="unknown",
        speaker_evidence_id=None,
    )
    return SpokenFinal(offset, entry, 0, None)


def test_the_judge_decides_the_recorded_delay_after_the_final() -> None:
    finals = (spoken("t1", "can you see my screen", 2.0),)

    async def main() -> tuple[float, Any]:
        loop = asyncio.get_running_loop()
        await asyncio.sleep(2.0)
        judgement = await RecordedJudge({"t1": reply_verdict()}, finals).judge(
            JudgeWindow((), JudgeLine("ayu", "can you see my screen"), transcript_id="t1")
        )
        return loop.time(), judgement

    elapsed, judgement = run_virtual(main)

    assert elapsed == pytest.approx(2.4)
    assert judgement.action == "reply"
    assert judgement.addressee == "twin"
    assert judgement.confidence == pytest.approx(0.9)
    assert judgement.evidence == ("screen",)


def test_an_interim_with_the_final_words_gets_the_final_verdict_on_time() -> None:
    finals = (spoken("t1", "Can you see my screen?", 2.0),)

    async def main() -> tuple[float, Any]:
        loop = asyncio.get_running_loop()
        await asyncio.sleep(1.5)
        judgement = await RecordedJudge({"t1": reply_verdict()}, finals).judge(
            JudgeWindow((), JudgeLine("speaking", "can you see my screen"))
        )
        return loop.time(), judgement

    elapsed, judgement = run_virtual(main)

    assert elapsed == pytest.approx(2.4)
    assert judgement.action == "reply"


def test_a_partial_interim_is_judged_unfinished() -> None:
    finals = (spoken("t1", "can you see my screen", 2.0),)

    async def main() -> Any:
        return await RecordedJudge({"t1": reply_verdict()}, finals).judge(
            JudgeWindow((), JudgeLine("speaking", "can you"))
        )

    judgement = run_virtual(main)

    assert judgement.finished is False
    assert judgement.action == "stay_silent"


def test_a_silenced_verdict_replays_as_staying_silent() -> None:
    verdict = JudgeVerdict(
        10.0,
        {
            "reason": "judge_below_threshold",
            "action": "silent",
            "confidence": 0.2,
            "heard": "mm",
            "addressee": "someone_else",
            "finished": True,
            "backchannel": False,
            "requests_chat": False,
            "meet_action": "none",
            "action_only": False,
            "logistics_check": False,
            "state_question": "none",
            "reaction_emoji": None,
            "evidence_needs": [],
        },
    )

    async def main() -> Any:
        return await RecordedJudge({"t1": verdict}).judge(
            JudgeWindow((), JudgeLine("ayu", "mm"), transcript_id="t1")
        )

    judgement = run_virtual(main)

    assert judgement.action == "stay_silent"


def replies() -> dict[str, ReplyTiming]:
    return {
        "t1": ReplyTiming(
            1500.0,
            (
                ReplySentence(500.0, 0, 26, "Yes, I can see it clearly."),
                ReplySentence(900.0, 1, 12, None),
            ),
            ("Yes, I can see it clearly.",),
        )
    }


def plans() -> dict[str, RecordedPlan]:
    return {
        "t1": RecordedPlan(
            language="en",
            intent="answer",
            contribution="none",
            cites=(),
            structured=True,
            replaced=False,
        )
    }


def test_the_model_streams_the_recorded_sentences_at_the_recorded_offsets() -> None:
    async def main() -> list[tuple[float, str]]:
        loop = asyncio.get_running_loop()
        model = RecordedModel(replies(), plans())
        marks: list[tuple[float, str]] = []
        async for chunk in model.astream(
            [HumanMessage("can you see my screen", id="t1")],
        ):
            marks.append((loop.time(), chunk.text))
        return marks

    marks = run_virtual(main)

    assert marks[0][0] == pytest.approx(0.0)
    assert marks[1][0] == pytest.approx(0.5)
    assert marks[2][0] == pytest.approx(0.9)
    assert marks[-1][0] == pytest.approx(1.5)


def test_the_model_writes_a_plan_the_planner_reads() -> None:
    async def main() -> list[str]:
        model = RecordedModel(replies(), plans())
        pieces: list[str] = []
        async for chunk in model.astream(
            [HumanMessage("can you see my screen", id="t1")],
        ):
            pieces.append(chunk.text)
        return pieces

    pieces = run_virtual(main)

    assert "".join(pieces).startswith("{")
    planner = ReplyPlanner(max_sentences=4, language="en")
    released: list[str] = []
    for piece in pieces:
        content = planner.feed(piece)
        if content:
            released.append(content)
    released.extend(planner.finish_sentences())

    assert released[0].startswith("Yes, I can see it clearly.")
    assert planner.language == "en"
    assert planner.intent == "answer"


def test_the_model_says_nothing_for_a_turn_without_a_recorded_reply() -> None:
    async def main() -> str:
        model = RecordedModel(replies(), plans())
        pieces = []
        async for chunk in model.astream([HumanMessage("hello", id="t9")]):
            pieces.append(chunk.text)
        return "".join(pieces)

    raw = run_virtual(main)

    assert '"intent": "silent"' in raw


def test_the_pacer_plays_the_audio_at_the_pcm_rate() -> None:
    async def main() -> float:
        loop = asyncio.get_running_loop()
        backend = create_pacer_backend()

        async def chunks() -> AsyncIterator[bytes]:
            yield b"\x00" * 4800
            yield b"\x00" * 4800

        await backend.stream_audio(chunks())
        return loop.time()

    elapsed = run_virtual(main)

    assert elapsed == pytest.approx(9600 / BYTES_PER_SECOND)


def test_a_chunk_that_arrives_late_leaves_a_gap_before_it_plays() -> None:
    timings = {("t1", 0): SynthesisTiming(300.0, 900.0, 9600, 26)}

    async def main() -> float:
        loop = asyncio.get_running_loop()
        backend = create_pacer_backend()
        stream = RecordedSynthesizer(timings).stream("hello", sentence_index=0, transcript_id="t1")
        await backend.stream_audio(stream)
        return loop.time()

    elapsed = run_virtual(main)

    assert elapsed == pytest.approx(0.9 + 4800 / BYTES_PER_SECOND)


def test_the_pacer_takes_the_audio_faster_than_it_plays_it() -> None:
    received: list[float] = []

    async def main() -> float:
        loop = asyncio.get_running_loop()

        async def chunks() -> AsyncIterator[bytes]:
            for _ in range(10):
                yield b"\x00" * 4800
            received.append(loop.time())

        await create_pacer_backend().stream_audio(chunks())
        return loop.time()

    elapsed = run_virtual(main)

    assert received == [pytest.approx(0.0)]
    assert elapsed == pytest.approx(48000 / BYTES_PER_SECOND)


def test_the_pacer_plays_the_audio_longer_than_the_provider_took() -> None:
    timings = {("t1", 0): SynthesisTiming(300.0, 400.0, 48000, 26)}

    async def main() -> float:
        loop = asyncio.get_running_loop()
        backend = create_pacer_backend()
        stream = RecordedSynthesizer(timings).stream("hello", sentence_index=0, transcript_id="t1")
        await backend.stream_audio(stream)
        return loop.time()

    elapsed = run_virtual(main)

    assert elapsed == pytest.approx(0.3 + 48000 / BYTES_PER_SECOND)


def test_the_pacer_needs_no_sound_device() -> None:
    async def main() -> None:
        await create_pacer_backend().set_muted(muted=True)

    run_virtual(main)


def test_the_replay_fakes_run_without_real_time() -> None:
    async def main() -> float:
        loop = asyncio.get_running_loop()
        await asyncio.sleep(600)
        return loop.time()

    started = time.monotonic()
    elapsed = run_virtual(main)

    assert elapsed == pytest.approx(600)
    assert time.monotonic() - started < 1


RUNTIME = Path(".runtime/audio")


@pytest.mark.skipif(not (RUNTIME / "journals/live-oue-4.jsonl").exists(), reason="no oue-4 data")
def test_the_real_agent_replays_a_recorded_reply_at_its_recorded_offset() -> None:
    fixture = load_fixture(
        RUNTIME / "journals/live-oue-4.jsonl", RUNTIME / "meetings/live-oue-4/record.jsonl"
    )
    transcript_id, reply = next(
        (key, value)
        for key, value in fixture.replies.items()
        if len(value.sentences) == 1 and value.sentences[0].text is not None
    )
    agent = create_meeting_agent(
        RecordedModel(fixture.replies, fixture.plans), tools=(), persona="You are a twin."
    )

    async def main() -> list[tuple[float, str]]:
        loop = asyncio.get_running_loop()
        marks: list[tuple[float, str]] = []
        async for message, metadata in agent.astream(
            {"messages": [{"role": "user", "content": "can you see", "id": transcript_id}]},
            stream_mode="messages",
        ):
            if isinstance(metadata, dict) and metadata.get("langgraph_node") == "model":
                marks.append((loop.time(), str(getattr(message, "text", ""))))
        return marks

    marks = run_virtual(main)
    sentence = reply.sentences[0]

    assert sentence.text is not None
    assert marks[1][0] == pytest.approx(sentence.offset_ms / 1000)
    assert sentence.text in marks[1][1]
    assert reply.duration_ms is not None
    assert marks[-1][0] == pytest.approx(reply.duration_ms / 1000)


@pytest.mark.skipif(not (RUNTIME / "journals/live-oue-4.jsonl").exists(), reason="no oue-4 data")
def test_the_oue_4_session_replays_its_recorded_latencies() -> None:
    fixture = load_fixture(
        RUNTIME / "journals/live-oue-4.jsonl", RUNTIME / "meetings/live-oue-4/record.jsonl"
    )
    transcript_id, reply = next(
        (key, value)
        for key, value in fixture.replies.items()
        if len(value.sentences) == 1 and value.sentences[0].text is not None
    )

    async def main() -> list[tuple[float, str]]:
        loop = asyncio.get_running_loop()
        model = RecordedModel(fixture.replies, fixture.plans)
        marks: list[tuple[float, str]] = []
        async for chunk in model.astream([HumanMessage("hello", id=transcript_id)]):
            marks.append((loop.time(), chunk.text))
        return marks

    marks = run_virtual(main)
    sentence = reply.sentences[0]

    assert marks[1][0] == pytest.approx(sentence.offset_ms / 1000)
    assert reply.duration_ms is not None
    assert marks[-1][0] == pytest.approx(reply.duration_ms / 1000)
    assert sentence.text is not None
    assert sentence.text in "".join(piece for _, piece in marks)


def test_a_recorded_fault_fails_only_the_attempt_it_failed() -> None:
    faults = {("t1", 0): (SynthesisFault(100.0, "timeout"),)}
    timings = {("t1", 0): SynthesisTiming(300.0, 900.0, 9600, 26)}
    synthesizer = RecordedSynthesizer(timings, faults=faults)

    async def main() -> int:
        with pytest.raises(ProviderFault):
            await drain(synthesizer.stream("hello", sentence_index=0, transcript_id="t1"))
        retried = await drain(synthesizer.stream("hello", sentence_index=0, transcript_id="t1"))
        return sum(size for _, size in retried)

    assert run_virtual(main) == 9600
