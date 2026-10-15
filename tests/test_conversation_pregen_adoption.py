import asyncio
from collections.abc import AsyncIterator, Callable, Sequence

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import (
    ConversationLoop,
    LoopGeneration,
    StreamingLangGraphResponder,
)
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import ToolCallingFakeModel
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal
from tests._pregen_support import StreamingResponder, journal_has


async def test_pregeneration_adopted_defers_speech_until_consumer_runs(
    journal: RecordingJournal,
) -> None:
    responder = StreamingResponder(["Yes I can.", "Let me check."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    await loop.handle_transcript(event("f1", "can you help me today?", finalized=True))
    assert audio.spoken == []
    await loop.wait_idle()
    assert audio.spoken == ["Yes I can.", "Let me check."]
    assert len(responder.calls) == 1
    assert journal_has(journal, "pregeneration_finished", outcome="adopted")
    await loop.close()


async def test_pregen_stream_writes_to_scratch_thread_only() -> None:
    agent = create_meeting_agent(
        ToolCallingFakeModel(responses=[AIMessage(content="Interim guess here.")]),
        checkpointer=InMemorySaver(),
    )
    responder = StreamingLangGraphResponder(agent, "thread")
    stream = responder.stream_pregen_sentences(
        [event("pregen-1", "interim guess")], lambda: None, request_id="pregen-1:0"
    )
    assert [sentence async for sentence in stream] == ["Interim guess here."]
    real = await agent.aget_state({"configurable": {"thread_id": "thread"}})
    assert real.values.get("messages", []) == []
    scratch_id = responder.scratch_thread_id("pregen-1:0")
    assert scratch_id != "thread"
    scratch = await agent.aget_state({"configurable": {"thread_id": scratch_id}})
    assert [message.content for message in scratch.values["messages"]] == [
        "interim guess",
        "Interim guess here.",
    ]


async def test_complete_adopted_pregen_moves_final_exchange_to_real_thread() -> None:
    agent = create_meeting_agent(
        ToolCallingFakeModel(responses=[]),
        checkpointer=InMemorySaver(),
    )
    responder = StreamingLangGraphResponder(agent, "thread")
    await responder.complete_adopted_pregen(
        [event("f1", "final words")], ["Yes I can.", "Let me check."]
    )
    state = await agent.aget_state({"configurable": {"thread_id": "thread"}})
    [human, answer] = state.values["messages"]
    assert isinstance(human, HumanMessage)
    assert (human.id, human.content) == ("f1", "final words")
    assert isinstance(answer, AIMessage)
    assert answer.content == "Yes I can. Let me check."


async def test_discarded_pregen_leaves_no_trace_in_real_thread(
    journal: RecordingJournal,
) -> None:
    agent = create_meeting_agent(
        ToolCallingFakeModel(
            responses=[
                AIMessage(content="Pregen guess."),
                AIMessage(content="Real answer."),
            ]
        ),
        checkpointer=InMemorySaver(),
    )
    responder = StreamingLangGraphResponder(agent, "thread", journal=journal)
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    await loop.handle_transcript(event("f1", "cancel everything now", finalized=True))
    await loop.wait_idle()
    assert audio.spoken == ["Real answer."]
    state = await agent.aget_state({"configurable": {"thread_id": "thread"}})
    humans = [
        message.content for message in state.values["messages"] if isinstance(message, HumanMessage)
    ]
    assert humans == ["cancel everything now"]
    await loop.close()


async def test_adopted_pregen_moves_final_exchange_to_real_thread(
    journal: RecordingJournal,
) -> None:
    agent = create_meeting_agent(
        ToolCallingFakeModel(responses=[AIMessage(content="Yes I can. Let me check.")]),
        checkpointer=InMemorySaver(),
    )
    responder = StreamingLangGraphResponder(agent, "thread", journal=journal)
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    await loop.handle_transcript(event("f1", "can you help me today?", finalized=True))
    await loop.wait_idle()
    assert audio.spoken == ["Yes I can.", "Let me check."]
    state = await agent.aget_state({"configurable": {"thread_id": "thread"}})
    [human, answer] = state.values["messages"]
    assert isinstance(human, HumanMessage)
    assert (human.id, human.content) == ("f1", "can you help me today?")
    assert isinstance(answer, AIMessage)
    assert answer.content == "Yes I can. Let me check."
    await loop.close()


class _DroppingAfterAdoptionResponder:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.merged: list[tuple[str, ...]] = []

    def stream_sentences(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
    ) -> AsyncIterator[str]:
        del events
        del on_tool_start
        del request_id
        return self._generate()

    async def _generate(self) -> AsyncIterator[str]:
        yield "Yes I can."
        await self.release.wait()
        raise RuntimeError("provider dropped the stream")

    async def complete_adopted_pregen(
        self, events: Sequence[TranscriptEvent], sentences: tuple[str, ...]
    ) -> None:
        del events
        self.merged.append(sentences)


async def test_adopted_pregen_stream_failure_fails_the_turn(journal: RecordingJournal) -> None:
    responder = _DroppingAfterAdoptionResponder()
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    await loop.handle_transcript(event("f1", "can you help me today?", finalized=True))
    assert journal_has(journal, "pregeneration_finished", outcome="adopted")
    responder.release.set()

    await loop.wait_idle()

    loop.raise_if_failed()
    assert [row["error"] for row in journal.events("turn_failed")] == ["RuntimeError"]
    assert audio.spoken[0] == "Yes I can."
    assert len(audio.spoken) == 2
    assert responder.merged == []
    [adopted] = journal.events("pregeneration_finished")
    assert journal_has(journal, "agent_failed", request_id=adopted["request_id"])
    assert not journal.events("agent_finished")
    await loop.close()
