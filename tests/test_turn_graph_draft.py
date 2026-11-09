import asyncio
from collections.abc import AsyncIterator
from typing import Any, override

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.conversation.participation_graph import JudgeThresholds, TurnJudging
from fakeyou.conversation.reply_draft import HeldEffects, ReplyDrafts
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_graph import (
    Responder,
    ToolStartHook,
    TurnInput,
    build_speech_graph,
    build_turn_graph,
)
from fakeyou.conversation.turn_judge import (
    JudgeCache,
    JudgeWindow,
    TurnJudgement,
    WindowSource,
)
from fakeyou.conversation.turn_queue import TurnEvent
from fakeyou.foundation.participation import ParticipationContext
from fakeyou.foundation.speakers import UNIDENTIFIED_PARTICIPANT
from fakeyou.foundation.transcript import TranscriptEvent
from tests._conversation_fakes import judged
from tests._prepared_support import PreparedSpeaker
from tests._turn_context import TurnTestContext

TWIN_REPLY = "Yes, I am here."
QUESTION = "twin, are you there?"
FINAL_JUDGE_SECONDS = 0.05


class Speaker(PreparedSpeaker):
    def __init__(self) -> None:
        self.utterances: list[str] = []
        self.is_speaking = False

    @override
    async def speak(self, utterance: str, **_: Any) -> None:
        self.utterances.append(utterance)

    async def stop_playback(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def raise_if_failed(self) -> None:
        return None

    async def wait_failed(self) -> None:
        await asyncio.Event().wait()


class Journal:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def record(self, event: str, *, source: str, **fields: Any) -> None:
        self.rows.append((event, {"source": source, **fields}))

    def reasons(self, event: str) -> list[object]:
        return [fields.get("reason") for name, fields in self.rows if name == event]

    def has(self, event: str) -> bool:
        return any(name == event for name, _ in self.rows)


class Model:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.held: list[bool] = []

    def __call__(
        self,
        batch: tuple[TranscriptEvent, ...],
        hook: ToolStartHook,
        request_id: str,
        /,
        *,
        effects: HeldEffects | None = None,
    ) -> Responder:
        del batch, hook
        self.requests.append(request_id)
        self.held.append(effects is not None)
        return self

    async def stream_reply(self, transcript: str) -> AsyncIterator[SpokenChunk]:
        del transcript
        yield SpokenChunk(text=TWIN_REPLY, language=DEFAULTS.speech.language)


class History:
    def __init__(self) -> None:
        self.lines: list[Any] = []

    def entries(self) -> list[Any]:
        return self.lines


class SlowFinalJudge:
    def __init__(self, final: TurnJudgement, history: History | None = None) -> None:
        self._final = final
        self._history = history

    async def judge(self, window: JudgeWindow) -> TurnJudgement:
        if window.latest.speaker == UNIDENTIFIED_PARTICIPANT:
            return judged("reply")
        await asyncio.sleep(FINAL_JUDGE_SECONDS)
        if self._history is not None:
            self._history.lines.append(object())
        return self._final


def runtime(
    final: TurnJudgement, journal: Journal, model: Model, history: History | None = None
) -> TurnTestContext:
    return TurnTestContext(
        speaker=Speaker(),
        journal=journal,
        stream_turn=model,
        judging=TurnJudging(
            cache=JudgeCache(SlowFinalJudge(final, history)),
            windows=WindowSource(history or NoHistory(), window_lines=8, twin_names=("twin",)),
            thresholds=JudgeThresholds(reply=0.6, clarify=0.5),
        ),
        speech=build_speech_graph(),
        drafts=ReplyDrafts(journal),
    )


def final_input(text: str = QUESTION) -> TurnInput:
    return TurnInput(
        event=TurnEvent(kind="final", event_id="e1", at=1.0, payload=text),
        context=ParticipationContext(addressed=False),
        speaking_generation=4,
        transcript_event=TranscriptEvent(
            id="e1",
            timestamp="2026-11-08T10:00:00+00:00",
            source="meeting_audio",
            text=text,
            finalized=True,
            participant_id="Ana",
            speaker_evidence_id=None,
        ),
    )


def interim_input(text: str) -> TurnInput:
    return TurnInput(
        event=TurnEvent(kind="interim", event_id="e0", at=0.5, payload=text),
        context=ParticipationContext(addressed=False),
        speaking_generation=4,
    )


async def converse(context: TurnTestContext, interim: str, final: str = QUESTION) -> None:
    graph = build_turn_graph()
    await graph.run(interim_input(interim), context=context)
    await asyncio.sleep(0)
    await graph.run(final_input(final), context=context)
    await context.start_pending()
    assert context.drafts is not None
    await context.drafts.close()


async def test_an_admitted_turn_speaks_the_draft_started_before_the_judge_finished() -> None:
    journal, model = Journal(), Model()
    context = runtime(judged("reply"), journal, model)

    await converse(context, "twin are you")

    assert isinstance(context.speaker, Speaker)
    assert context.speaker.utterances == [TWIN_REPLY]
    assert model.requests == ["e1:draft"]
    assert model.held == [True]
    assert journal.has("reply_draft_accepted")


async def test_a_turn_the_judge_silences_never_speaks_its_draft() -> None:
    journal, model = Journal(), Model()
    context = runtime(judged("stay_silent"), journal, model)

    await converse(context, "twin are you")

    assert isinstance(context.speaker, Speaker)
    assert context.speaker.utterances == []
    assert model.requests == ["e1:draft"]
    assert journal.reasons("reply_draft_dropped") == ["not_admitted"]


async def test_a_turn_that_needs_a_look_answers_afresh_and_drops_the_draft() -> None:
    journal, model = Journal(), Model()
    context = runtime(judged("reply", evidence=("screen",)), journal, model)

    await converse(context, "twin are you")

    assert isinstance(context.speaker, Speaker)
    assert context.speaker.utterances == [TWIN_REPLY]
    assert model.requests[0] == "e1:draft"
    assert len(model.requests) == 2
    assert journal.reasons("reply_draft_dropped") == ["key_mismatch"]


async def test_a_line_that_does_not_name_the_twin_starts_no_draft() -> None:
    journal, model = Journal(), Model()
    context = runtime(judged("reply"), journal, model)

    await converse(context, "are you", final="are you there?")

    assert not journal.has("reply_draft_started")
    assert "e1:draft" not in model.requests


async def test_a_line_said_while_the_judge_decided_makes_the_turn_answer_afresh() -> None:
    journal, model = Journal(), Model()
    context = runtime(judged("reply"), journal, model, History())

    await converse(context, "twin are you")

    assert isinstance(context.speaker, Speaker)
    assert context.speaker.utterances == [TWIN_REPLY]
    assert model.held == [True, False]
    assert journal.reasons("reply_draft_dropped") == ["key_mismatch"]


async def test_a_turn_without_evidence_needs_still_speaks_the_draft() -> None:
    journal, model = Journal(), Model()
    context = runtime(judged("reply", evidence=()), journal, model)

    await converse(context, "twin are you")

    assert isinstance(context.speaker, Speaker)
    assert context.speaker.utterances == [TWIN_REPLY]
    assert model.requests == ["e1:draft"]
    assert journal.has("reply_draft_accepted")


async def test_a_draft_starts_on_the_final_without_any_earlier_judgement() -> None:
    journal, model = Journal(), Model()
    context = runtime(judged("reply"), journal, model)
    graph = build_turn_graph()

    await graph.run(final_input(), context=context)
    await context.start_pending()
    assert context.drafts is not None
    await context.drafts.close()

    assert model.requests == ["e1:draft"]
    assert journal.has("reply_draft_accepted")
