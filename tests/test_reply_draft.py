import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from fakeyou.conversation.reply_draft import (
    DraftKey,
    DraftRequest,
    HeldEffects,
    ReplyDraft,
    ReplyDrafts,
    ToolUse,
)
from fakeyou.conversation.reply_planner import ReplyIntent, SpokenChunk

KEY = DraftKey(("e1",), "twin, are you there?", (), owner_claim=False, history=3)
OTHER_KEY = DraftKey(("e1",), "twin, are you there?", ("memory",), owner_claim=False, history=3)


class Journal:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any]]] = []

    def record(self, event: str, *, source: str, **fields: Any) -> None:
        self.rows.append((event, {"source": source, **fields}))

    def reasons(self, event: str) -> list[object]:
        return [fields.get("reason") for name, fields in self.rows if name == event]


class Model:
    def __init__(self, *texts: str, fail: Exception | None = None) -> None:
        self.texts = texts
        self.fail = fail
        self.release = asyncio.Event()
        self.cancelled = False

    async def stream(self) -> AsyncIterator[SpokenChunk]:
        try:
            for index, text in enumerate(self.texts):
                if index:
                    await self.release.wait()
                yield SpokenChunk(text=text, language="en")
            if self.fail is not None:
                raise self.fail
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class Control:
    def __init__(self) -> None:
        self.applied: list[str] = []

    def intent(self, intent: ReplyIntent) -> str | None:
        self.applied.append(intent)
        return "last line" if intent == "repeat" else None

    def language(self, language: str) -> None:
        self.applied.append(f"language:{language}")


def request(
    model: Model,
    key: DraftKey = KEY,
    effects: HeldEffects | None = None,
    tool: ToolUse | None = None,
) -> DraftRequest:
    return DraftRequest(
        key,
        f"{key.transcript_ids[-1]}:draft",
        model.stream(),
        effects or HeldEffects(),
        tool or ToolUse(),
    )


async def opened(drafts: ReplyDrafts, draft_request: DraftRequest) -> None:
    async def opener() -> DraftRequest:
        return draft_request

    drafts.start(draft_request.key.transcript_ids[-1], opener)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_a_draft_replays_what_it_buffered_and_then_what_arrives_live() -> None:
    model = Model("First sentence.", "Second sentence.")
    draft = ReplyDraft(request(model))
    await asyncio.sleep(0.01)

    heard: list[str] = []

    async def listen() -> None:
        async for chunk in draft.replay():
            heard.append(chunk.text)

    listening = asyncio.create_task(listen())
    await asyncio.sleep(0.01)
    assert heard == ["First sentence."]
    model.release.set()
    await listening

    assert heard == ["First sentence.", "Second sentence."]


async def test_a_draft_raises_the_model_failure_to_the_turn_that_uses_it() -> None:
    draft = ReplyDraft(request(Model("Partial.", fail=TimeoutError())))

    with pytest.raises(TimeoutError):
        async for _chunk in draft.replay():
            pass


async def test_a_turn_that_stops_listening_cancels_the_model_stream() -> None:
    model = Model("First sentence.", "Never heard.")
    draft = ReplyDraft(request(model))
    replay = draft.replay()

    assert (await anext(replay)).text == "First sentence."
    await replay.aclose()

    assert model.cancelled


async def test_a_taken_draft_that_never_replays_can_still_be_cancelled() -> None:
    model = Model("First sentence.", "Never heard.")
    draft = ReplyDraft(request(model))
    await asyncio.sleep(0.01)

    await draft.cancel()

    assert model.cancelled


def test_held_effects_change_nothing_until_released() -> None:
    control = Control()
    effects = HeldEffects()
    effects.bind(control.intent, control.language)

    assert effects.intent("louder") is None
    effects.language("en")
    assert control.applied == []

    effects.release()

    assert control.applied == ["louder", "language:en"]


def test_a_repeat_is_answered_while_drafting_because_it_only_reads() -> None:
    control = Control()
    effects = HeldEffects()
    effects.bind(control.intent, control.language)

    assert effects.intent("repeat") == "last line"


async def test_a_dropped_draft_never_applies_its_effects() -> None:
    control = Control()
    effects = HeldEffects()
    effects.bind(control.intent, control.language)
    effects.intent("shorter")
    drafts = ReplyDrafts(Journal())
    await opened(drafts, request(Model("Yes."), effects=effects))

    assert drafts.take(OTHER_KEY) is None
    await drafts.close()

    assert control.applied == []


async def test_an_accepted_draft_applies_its_effects_and_forwards_tool_use() -> None:
    control = Control()
    effects = HeldEffects()
    effects.bind(control.intent, control.language)
    effects.intent("shorter")
    tool = ToolUse()
    tool.start()
    drafts = ReplyDrafts(Journal())
    await opened(drafts, request(Model("Yes."), effects=effects, tool=tool))
    turn_tools: list[str] = []

    draft = drafts.take(KEY)
    assert draft is not None
    draft.accept(lambda: turn_tools.append("started"))
    tool.start()

    assert control.applied == ["shorter"]
    assert turn_tools == ["started", "started"]
    await drafts.close()


async def test_a_matching_turn_takes_the_draft_and_journals_it() -> None:
    journal = Journal()
    drafts = ReplyDrafts(journal)
    await opened(drafts, request(Model("Yes, I am here.")))

    draft = drafts.take(KEY)

    assert draft is not None
    assert [name for name, _ in journal.rows] == ["reply_draft_started", "reply_draft_accepted"]
    assert journal.rows[1][1]["request_id"] == "e1:draft"
    await drafts.close()


async def test_a_turn_planned_differently_drops_the_draft_and_cancels_the_model() -> None:
    journal = Journal()
    drafts = ReplyDrafts(journal)
    model = Model("Yes, I am here.", "Never heard.")
    await opened(drafts, request(model))

    assert drafts.take(OTHER_KEY) is None
    await drafts.close()

    assert journal.reasons("reply_draft_dropped") == ["key_mismatch"]
    assert model.cancelled


async def test_a_turn_admitted_before_the_draft_opened_does_not_wait_for_it() -> None:
    journal = Journal()
    drafts = ReplyDrafts(journal)
    never = asyncio.Event()

    async def opener() -> DraftRequest:
        await never.wait()
        return request(Model("Too late."))

    drafts.start("e1", opener)

    assert drafts.take(KEY) is None
    assert journal.reasons("reply_draft_dropped") == ["not_ready"]


async def test_a_batch_that_contains_the_drafted_final_releases_its_draft() -> None:
    journal = Journal()
    drafts = ReplyDrafts(journal)
    model = Model("Yes, I am here.", "Never heard.")
    await opened(drafts, request(model))

    drafts.drop(["e1", "e2"], "unused")
    await drafts.close()

    assert journal.reasons("reply_draft_dropped") == ["unused"]
    assert model.cancelled


async def test_a_new_utterance_supersedes_the_previous_draft() -> None:
    journal = Journal()
    drafts = ReplyDrafts(journal)
    first = Model("Old answer.", "Never heard.")
    await opened(drafts, request(first))

    await opened(
        drafts,
        request(Model("New answer."), DraftKey(("e2",), "next", (), owner_claim=False, history=4)),
    )
    await drafts.close()

    assert journal.reasons("reply_draft_dropped") == ["superseded", "closed"]
    assert first.cancelled


async def test_dropping_another_utterance_keeps_the_current_draft() -> None:
    drafts = ReplyDrafts(Journal())
    await opened(drafts, request(Model("Yes, I am here.")))

    drafts.drop(["e0"], "not_admitted")

    assert drafts.take(KEY) is not None
    await drafts.close()


async def test_closing_survives_an_opener_that_failed() -> None:
    drafts = ReplyDrafts(Journal())

    async def opener() -> DraftRequest:
        raise RuntimeError("planning failed")

    drafts.start("e1", opener)
    await asyncio.sleep(0)

    await drafts.close()
