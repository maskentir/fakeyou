from collections.abc import Sequence
from datetime import UTC, datetime
from typing import override

from fakeyou.conversation.contribution import OwnerModelSource, RecalledFact, source_registry
from fakeyou.conversation.moments import MomentCandidate, MomentDetector, MomentJudgement
from fakeyou.foundation.transcript import TranscriptEvent

FACT = "The Orchid beta launch is planned for 14 October 2026"
QUESTION = "When does the beta launch?"


class _Embedder:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    async def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vectors.get(text, [0.0, 1.0]) for text in texts]


class _Judge:
    def __init__(self, judgement: MomentJudgement) -> None:
        self._judgement = judgement
        self.calls = 0

    async def judge(
        self,
        *,
        lines: Sequence[tuple[str, str]],
        candidates: Sequence[MomentCandidate],
        owner_names: Sequence[str],
        twin_names: Sequence[str],
    ) -> MomentJudgement:
        del lines, candidates, owner_names, twin_names
        self.calls += 1
        return self._judgement


def _event(event_id: str, text: str, participant: str = "device-ayu") -> TranscriptEvent:
    return TranscriptEvent(
        id=event_id,
        timestamp=datetime.now(UTC).isoformat(),
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id=participant,
        speaker_evidence_id=None,
    )


def _detector(judgement: MomentJudgement, judge: _Judge | None = None) -> MomentDetector:
    registry = source_registry(owner_model={"om-beta": OwnerModelSource(FACT, "room_speakable")})
    embedder = _Embedder(
        {
            FACT: [1.0, 0.0],
            QUESTION: [0.95, 0.05],
            f"Rani Twin, {QUESTION.lower()}": [0.95, 0.05],
            f"Rani, {QUESTION.lower()}": [0.95, 0.05],
            "unrelated": [0.0, 1.0],
        }
    )
    return MomentDetector(
        registry=registry,
        judge=judge if judge is not None else _Judge(judgement),
        embed=embedder,
        twin_names=("Rani Twin",),
        owner_names=("Rani",),
        similarity_threshold=0.5,
        unanswered_seconds=15.0,
    )


async def test_a_pending_question_signals_readiness_and_its_maturation_time() -> None:
    detector = _detector(
        MomentJudgement(moment="unanswered_question", source_ref="om-beta", confidence=0.9)
    )
    signalled: list[bool] = []
    detector.on_ready = lambda: signalled.append(True)
    await detector.prepare()

    assert await detector.observe(_event("t1", QUESTION), now=100.0) is None

    assert signalled == [True]
    assert detector.has_ready() is True
    assert detector.ready_at() == 115.0
    assert detector.take_ready(now=115.0) is not None
    assert detector.has_ready() is False
    assert detector.ready_at() is None


async def test_an_immediate_moment_signals_readiness_without_a_maturation_time() -> None:
    detector = _detector(
        MomentJudgement(moment="fact_opportunity", source_ref="om-beta", confidence=0.9)
    )
    signalled: list[bool] = []
    detector.on_ready = lambda: signalled.append(True)
    await detector.prepare()

    assert await detector.observe(_event("t1", QUESTION), now=100.0) is not None

    assert signalled == [True]
    assert detector.has_ready() is True
    assert detector.ready_at() is None


async def test_an_unanswered_question_fires_only_after_the_wait() -> None:
    detector = _detector(
        MomentJudgement(moment="unanswered_question", source_ref="om-beta", confidence=0.9)
    )
    await detector.prepare()

    assert await detector.observe(_event("t1", QUESTION), now=100.0) is None
    assert detector.poll(now=110.0) is None
    context = detector.poll(now=116.0)

    assert context is not None
    assert context.kind == "unanswered_question"
    assert context.suggested_source_ref == "om-beta"
    assert detector.poll(now=200.0) is None


async def test_a_line_naming_the_twin_is_left_to_the_normal_lane() -> None:
    judgement = MomentJudgement(moment="unanswered_question", source_ref="om-beta", confidence=0.9)
    judge = _Judge(judgement)
    detector = _detector(judgement, judge=judge)
    await detector.prepare()

    named = await detector.observe(_event("t1", f"Rani Twin, {QUESTION.lower()}"), now=100.0)

    assert named is None
    assert judge.calls == 0
    assert detector.poll(now=200.0) is None


async def test_a_line_addressing_the_owner_by_name_is_left_to_the_normal_lane() -> None:
    judgement = MomentJudgement(moment="unanswered_question", source_ref="om-beta", confidence=0.9)
    judge = _Judge(judgement)
    detector = _detector(judgement, judge=judge)
    await detector.prepare()

    addressed = await detector.observe(_event("t1", f"Rani, {QUESTION.lower()}"), now=100.0)

    assert addressed is None
    assert judge.calls == 0


async def test_a_line_naming_the_twin_cancels_a_pending_question() -> None:
    detector = _detector(
        MomentJudgement(moment="unanswered_question", source_ref="om-beta", confidence=0.9)
    )
    await detector.prepare()
    await detector.observe(_event("t1", QUESTION), now=100.0)
    await detector.observe(_event("t2", "Rani Twin, please answer that"), now=105.0)

    assert detector.poll(now=200.0) is None


async def test_an_answer_from_another_participant_cancels_the_question() -> None:
    detector = _detector(
        MomentJudgement(moment="unanswered_question", source_ref="om-beta", confidence=0.9)
    )
    await detector.prepare()
    await detector.observe(_event("t1", QUESTION), now=100.0)
    await detector.observe(_event("t2", "unrelated", participant="device-budi"), now=105.0)

    assert detector.poll(now=200.0) is None


async def test_a_decision_against_the_brief_fires_immediately_once() -> None:
    detector = _detector(
        MomentJudgement(moment="decision_vs_brief", source_ref="om-beta", confidence=0.9)
    )
    await detector.prepare()

    first = await detector.observe(_event("t1", QUESTION), now=100.0)
    second = await detector.observe(_event("t2", QUESTION), now=101.0)

    assert first is not None
    assert first.kind == "decision_vs_brief"
    assert second is None


async def test_an_unrelated_line_never_reaches_the_judge() -> None:
    judgement = MomentJudgement(moment="fact_opportunity", source_ref="om-beta", confidence=0.9)
    judge = _Judge(judgement)
    detector = _detector(judgement, judge)
    await detector.prepare()

    assert await detector.observe(_event("t1", "unrelated"), now=100.0) is None
    assert judge.calls == 0


async def test_low_confidence_and_unknown_refs_do_not_fire() -> None:
    low = _detector(
        MomentJudgement(moment="fact_opportunity", source_ref="om-beta", confidence=0.5)
    )
    await low.prepare()
    assert await low.observe(_event("t1", QUESTION), now=100.0) is None

    unknown = _detector(
        MomentJudgement(moment="fact_opportunity", source_ref="nope", confidence=0.9)
    )
    await unknown.prepare()
    assert await unknown.observe(_event("t1", QUESTION), now=100.0) is None


async def test_without_an_embedder_no_moments_are_detected() -> None:
    detector = _detector(
        MomentJudgement(moment="fact_opportunity", source_ref="om-beta", confidence=0.9)
    )
    detector.embed = None
    await detector.prepare()

    assert await detector.observe(_event("t1", QUESTION), now=100.0) is None


async def test_a_screen_reading_reaches_the_same_judge_as_speech() -> None:
    judgement = MomentJudgement(moment="fact_opportunity", source_ref="om-beta", confidence=0.9)
    judge = _Judge(judgement)
    detector = _detector(judgement, judge)
    await detector.prepare()

    context = await detector.observe_screen(ref="screen#2", text=QUESTION, now=100.0)

    assert context is not None
    assert context.suggested_source_ref == "om-beta"
    assert judge.calls == 1


async def test_the_same_screen_reading_is_judged_only_once() -> None:
    judgement = MomentJudgement(moment="fact_opportunity", source_ref="om-beta", confidence=0.9)
    judge = _Judge(judgement)
    detector = _detector(judgement, judge)
    await detector.prepare()

    await detector.observe_screen(ref="screen#2", text=QUESTION, now=100.0)
    again = await detector.observe_screen(ref="screen#2", text=QUESTION, now=101.0)

    assert again is None
    assert judge.calls == 1


async def test_a_recalled_fact_becomes_a_moment_candidate_once_it_lands() -> None:
    judgement = MomentJudgement(moment="fact_opportunity", source_ref="mem-1", confidence=0.9)
    judge = _Judge(judgement)
    detector = _detector(judgement, judge)
    await detector.prepare()

    assert await detector.observe(_event("t1", "unrelated"), now=100.0) is None
    detector.note_recalled({"mem-1": RecalledFact(FACT)})
    context = await detector.observe(_event("t2", QUESTION), now=101.0)

    assert context is not None
    assert context.suggested_source == "memory"
    assert context.suggested_source_ref == "mem-1"


async def test_the_same_recalled_fact_is_embedded_only_once() -> None:
    embedded: list[str] = []

    class _CountingEmbedder(_Embedder):
        @override
        async def __call__(self, texts: Sequence[str]) -> list[list[float]]:
            embedded.extend(texts)
            return await super().__call__(texts)

    judgement = MomentJudgement(moment="none", confidence=0.9)
    detector = MomentDetector(
        registry=source_registry(),
        judge=_Judge(judgement),
        embed=_CountingEmbedder({FACT: [1.0, 0.0], QUESTION: [0.95, 0.05]}),
        similarity_threshold=0.5,
    )

    detector.note_recalled({"mem-1": RecalledFact(FACT)})
    await detector.observe(_event("t1", QUESTION), now=100.0)
    detector.note_recalled({"mem-1": RecalledFact(FACT)})
    await detector.observe(_event("t2", QUESTION), now=101.0)

    assert embedded.count(FACT) == 1


async def test_a_screen_reading_without_candidates_never_reaches_the_judge() -> None:
    judgement = MomentJudgement(moment="fact_opportunity", source_ref="om-beta", confidence=0.9)
    judge = _Judge(judgement)
    detector = _detector(judgement, judge)
    await detector.prepare()

    assert await detector.observe_screen(ref="screen#3", text="unrelated", now=100.0) is None
    assert judge.calls == 0
