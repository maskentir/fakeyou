import asyncio
import json
import uuid
from collections.abc import Iterable
from typing import Any, cast

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import (
    ConversationLoop,
    LoopGeneration,
    LoopInteraction,
    LoopTiming,
    StreamingLangGraphResponder,
    cap_spoken_reply,
)
from fakeyou.conversation.session import ConversationError
from fakeyou.memory.memory import create_recall_error_middleware
from fakeyou.observability.diagnostics import Diagnostics
from tests._conversation_support import event
from tests.fakes import (
    FakeReplyAudio,
    FakeResponder,
    RecordingJournal,
    ToolCallingFakeModel,
    recall_call,
)

_AgentT = Runnable[dict[str, list[HumanMessage]], dict[str, Any]]


class ScriptedStream:
    def __init__(
        self,
        owner: "ScriptedAgent",
        actions: Iterable[tuple[Any, ...]],
        callbacks: Iterable[Any],
    ) -> None:
        self._owner = owner
        self._actions = list(actions)
        self._callbacks = list(callbacks)
        self.delivered = 0
        self.aclosed = False

    def __aiter__(self) -> "ScriptedStream":
        return self

    async def __anext__(self) -> tuple[AIMessageChunk, dict[str, str]]:
        while self._actions:
            action = self._actions.pop(0)
            if action[0] == "chunk":
                self.delivered += 1
                self._owner.delivered += 1
                return AIMessageChunk(content=action[1]), {"langgraph_node": action[2]}
            if action[0] == "tool":
                run_id = uuid.uuid4()
                for callback in self._callbacks:
                    await callback.on_tool_start({"name": action[1]}, "", run_id=run_id)
                if isinstance(action[2], Exception):
                    for callback in self._callbacks:
                        await callback.on_tool_error(action[2], run_id=run_id)
                else:
                    outcome = ToolMessage(content=action[2], tool_call_id="call-1")
                    for callback in self._callbacks:
                        await callback.on_tool_end(outcome, run_id=run_id)
                continue
            if action[0] == "gate":
                action[1].set()
                await action[2].wait()
                continue
            if action[0] == "error":
                raise action[1]
            raise AssertionError(action[0])
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.aclosed = True
        self._owner.closed += 1


class ScriptedAgent:
    def __init__(
        self,
        scripts: dict[str, list[tuple[Any, ...]]] | list[tuple[Any, ...]],
        fallback_reply: str = "",
    ) -> None:
        self._scripts = scripts
        self.fallback_reply = fallback_reply
        self.streams: list[ScriptedStream] = []
        self.configs: list[dict[str, Any] | None] = []
        self.stream_modes: list[Any] = []
        self.delivered = 0
        self.closed = 0
        self.ainvoke_calls: list[dict[str, Any]] = []

    def astream(
        self, payload: dict[str, Any], config: dict[str, Any] | None = None, **kwargs: Any
    ) -> "ScriptedStream":
        key = payload["messages"][-1].id
        if isinstance(self._scripts, dict):
            actions = self._scripts.get(key, self._scripts.get("*", []))
        else:
            actions = self._scripts
        self.configs.append(config)
        self.stream_modes.append(kwargs.get("stream_mode"))
        stream = ScriptedStream(self, actions, (config or {}).get("callbacks", ()))
        self.streams.append(stream)
        return stream

    async def ainvoke(
        self, payload: dict[str, Any], config: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.ainvoke_calls.append(payload)
        return {"messages": [AIMessage(content=self.fallback_reply)]}


def streamed_loop(
    agent: Any,
    audio: FakeReplyAudio,
    journal: RecordingJournal,
    language: str = "id",
    **kwargs: Any,
) -> ConversationLoop:
    responder = StreamingLangGraphResponder(agent, "thread", journal=journal, language=language)
    interaction_keys = (
        "on_transition",
        "wake_phrases",
        "barge_in",
        "barge_in_min_words",
        "participant_policy_enabled",
    )
    timing_keys = ("response_timeout_seconds", "activation_seconds", "clock")
    generation_keys = (
        "max_spoken_sentences",
        "sentence_streaming",
        "pre_generate_enabled",
        "pregen_fuzzy_restart_enabled",
    )
    interaction = LoopInteraction(
        **{key: kwargs.pop(key) for key in interaction_keys if key in kwargs}
    )
    timing = LoopTiming(**{key: kwargs.pop(key) for key in timing_keys if key in kwargs})
    generation = LoopGeneration(
        language=language,
        sentence_streaming=True,
        **{key: kwargs.pop(key) for key in generation_keys if key in kwargs},
    )
    assert not kwargs, kwargs
    loop = ConversationLoop(
        responder,
        interaction=interaction,
        timing=timing,
        generation=generation,
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    return loop


async def test_streamed_reply_speaks_each_sentence_in_order(journal: RecordingJournal) -> None:
    agent = ScriptedAgent(
        [
            ("chunk", "First sent", "model"),
            ("chunk", "ence here. Second sent", "model"),
            ("chunk", "ence here.", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["First sentence here.", "Second sentence here."]
    assert " ".join(audio.spoken) == cap_spoken_reply("First sentence here. Second sentence here.")
    [started] = journal.events("agent_started")
    [first] = journal.events("answer_text_emitted")
    assert first["source"] == "llm"
    assert (first["request_id"], first["transcript_id"]) == (started["request_id"], "t1")
    emitted = journal.events("answer_sentence_emitted")
    assert [(row["sentence_index"], row["chars"]) for row in emitted] == [(0, 20), (1, 21)]
    assert {row["request_id"] for row in emitted} == {started["request_id"]}
    assert agent.stream_modes == ["messages"]
    assert journal.events("agent_finished") != []
    await loop.close()


@pytest.mark.parametrize("language", ["id", "en"])
async def test_streamed_reply_matches_cap_across_languages(
    journal: RecordingJournal, language: str
) -> None:
    agent = ScriptedAgent(
        [("chunk", "Morning update. All ", "model"), ("chunk", "systems ready.", "model")]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal, language=language)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["Morning update.", "All systems ready."]
    assert " ".join(audio.spoken) == cap_spoken_reply(
        "Morning update. All systems ready.", language=language
    )
    await loop.close()


async def test_streamed_reply_skips_tool_text_and_empty_chunks(journal: RecordingJournal) -> None:
    agent = ScriptedAgent(
        [
            ("chunk", "", "model"),
            ("chunk", "tool output secret", "tools"),
            ("chunk", "Real answer here. Done now.", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["Real answer here.", "Done now."]
    body = "\n".join(journal.lines)
    assert "tool output secret" not in body
    assert "Real answer here" not in body
    await loop.close()


async def test_streaming_responder_with_flag_off_uses_single_speak(
    journal: RecordingJournal,
) -> None:
    agent = ScriptedAgent([], fallback_reply="One. Two. Three.")
    audio = FakeReplyAudio()
    responder = StreamingLangGraphResponder(cast(_AgentT, agent), "thread", journal=journal)
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["One. Two."]
    assert journal.events("answer_text_emitted") == []
    assert journal.events("answer_sentence_emitted") == []
    assert len(agent.ainvoke_calls) == 1
    assert agent.streams == []
    await loop.close()


async def test_flag_on_with_plain_responder_keeps_old_behavior() -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, generation=LoopGeneration(sentence_streaming=True))
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["hello"]
    await loop.close()


async def test_streamed_reply_discards_partial_text_before_tool_end(
    journal: RecordingJournal,
) -> None:
    agent = ScriptedAgent(
        [
            ("chunk", "Checking memor", "model"),
            ("tool", "recall_meeting_context", "demo friday"),
            ("chunk", "The demo is on ", "model"),
            ("chunk", "Friday. Doors open at nine.", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "when is demo"))
    await loop.wait_idle()
    assert audio.spoken == ["The demo is on Friday.", "Doors open at nine."]
    body = "\n".join(journal.lines)
    assert "Checking memor" not in body
    assert "demo friday" not in body
    [started_tool] = journal.events("tool_started")
    assert (started_tool["tool"], started_tool["transcript_id"]) == ("recall_meeting_context", "t1")
    [finished_tool] = journal.events("tool_finished")
    assert finished_tool["transcript_id"] == "t1"
    await loop.close()


async def test_streamed_reply_marks_answer_text_after_tool_end(journal: RecordingJournal) -> None:

    agent = ScriptedAgent(
        [
            ("chunk", "", "model"),
            ("tool", "recall_meeting_context", "demo friday"),
            ("chunk", "The demo is on Friday.", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "when is demo"))
    await loop.wait_idle()
    assert audio.spoken == ["The demo is on Friday."]
    order = [json.loads(line)["event"] for line in journal.lines]
    assert order.index("answer_text_emitted") > order.index("tool_finished")
    await loop.close()


async def test_streamed_reply_survives_recall_failure(journal: RecordingJournal) -> None:
    from langchain_core.tools import tool

    @tool(description="Search long-term meeting memory for relevant context.")
    async def recall_meeting_context(query: str) -> str:
        raise RuntimeError("graph down")

    agent = create_meeting_agent(
        ToolCallingFakeModel(
            responses=[
                recall_call({"query": "demo"}, id="call-1"),
                AIMessage(content="I do not know the answer. Sorry about that."),
            ]
        ),
        tools=[recall_meeting_context],
        checkpointer=InMemorySaver(),
        middleware=[create_recall_error_middleware(None)],
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "when is demo"))
    await loop.wait_idle()
    assert audio.spoken == ["I do not know the answer.", "Sorry about that."]
    assert "Meeting memory is unavailable" not in " ".join(audio.spoken)
    [failed] = journal.events("tool_failed")
    assert (failed["tool"], failed["transcript_id"]) == ("recall_meeting_context", "t1")
    assert journal.events("agent_finished") != []
    await loop.close()


async def test_streamed_reply_uses_fallback_model(journal: RecordingJournal) -> None:
    from langchain.agents.middleware import ModelFallbackMiddleware

    class FailingChatModel(ToolCallingFakeModel):
        async def _agenerate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: AsyncCallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            raise RuntimeError("primary down")

    agent = create_meeting_agent(
        FailingChatModel(responses=[AIMessage(content="unused")]),
        checkpointer=InMemorySaver(),
        middleware=[
            ModelFallbackMiddleware(
                ToolCallingFakeModel(responses=[AIMessage(content="Fallback answer here.")])
            )
        ],
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["Fallback answer here."]
    assert journal.events("agent_finished") != []
    await loop.close()


async def test_streamed_reply_caps_third_sentence_and_stops_stream(
    journal: RecordingJournal,
) -> None:
    agent = ScriptedAgent(
        [("chunk", "One. Two. Three.", "model"), ("chunk", " Four. Five.", "model")]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["One.", "Two."]
    assert " ".join(audio.spoken) == cap_spoken_reply("One. Two. Three. Four. Five.")
    [capped] = journal.events("reply_capped")
    assert capped["chars_in"] >= capped["chars_out"] > 0
    assert agent.delivered == 1
    assert agent.streams[0].aclosed
    await loop.close()


async def test_streamed_reply_without_remainder_records_no_cap(journal: RecordingJournal) -> None:
    agent = ScriptedAgent([("chunk", "One. Two.", "model")])
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["One.", "Two."]
    assert journal.events("reply_capped") == []
    await loop.close()


async def test_streamed_empty_reply_moves_to_waiting(journal: RecordingJournal) -> None:
    agent = ScriptedAgent([])
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == []
    assert loop.phase == "waiting"
    await loop.close()


def test_streaming_responder_rejects_non_positive_cap() -> None:
    with pytest.raises(ValueError, match="max_sentences"):
        StreamingLangGraphResponder(cast(_AgentT, ScriptedAgent([])), "thread", max_sentences=0)


async def test_new_transcript_mid_stream_drops_remainder(journal: RecordingJournal) -> None:
    ready = asyncio.Event()
    release = asyncio.Event()
    agent = ScriptedAgent(
        {
            "t1": [
                ("chunk", "First done. ", "model"),
                ("gate", ready, release),
                ("chunk", "Second part here. ", "model"),
            ],
            "t2": [("chunk", "New answer.", "model")],
        }
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    await loop.handle_transcript(event("t2", "second"))
    release.set()
    await loop.wait_idle()
    assert audio.spoken == ["First done.", "New answer."]
    [dropped] = journal.events("sentence_dropped")
    assert (dropped["request_id"], dropped["transcript_id"]) == ("t1:1", "t1")
    assert (dropped["sentence_index"], dropped["detail"]) == (1, "stale_generation")
    assert agent.streams[0].aclosed
    await loop.close()


async def test_interim_stop_between_sentences_speaks_nothing_further(
    journal: RecordingJournal,
) -> None:
    ready = asyncio.Event()
    release = asyncio.Event()
    agent = ScriptedAgent(
        [
            ("chunk", "First done. ", "model"),
            ("gate", ready, release),
            ("chunk", "Second part here. ", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal, barge_in=True)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    audio.speaking = True
    await loop.handle_interim("wait just a moment")
    assert audio.stops == 1
    release.set()
    await loop.wait_idle()
    assert audio.spoken == ["First done."]
    [dropped] = journal.events("sentence_dropped")
    assert (dropped["transcript_id"], dropped["sentence_index"], dropped["detail"]) == (
        "t1",
        1,
        "stopped",
    )
    await loop.close()


async def test_close_during_stream_cancels_without_trailing_speech(
    journal: RecordingJournal,
) -> None:
    ready = asyncio.Event()
    release = asyncio.Event()
    agent = ScriptedAgent(
        [
            ("chunk", "First done. ", "model"),
            ("gate", ready, release),
            ("chunk", "Second part. ", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    await loop.close()
    assert audio.spoken == ["First done."]
    assert agent.streams[0].aclosed
    assert journal.events("agent_cancelled") != []


async def test_streamed_journal_events_carry_metadata_only(journal: RecordingJournal) -> None:
    agent = ScriptedAgent([("chunk", "First sentence here. Second sentence here.", "model")])
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    [first] = journal.events("answer_text_emitted")
    assert set(first) == {"event", "meeting_id", "at", "source", "request_id", "transcript_id"}
    emitted = journal.events("answer_sentence_emitted")
    assert [set(row) for row in emitted] == [
        {
            "event",
            "meeting_id",
            "at",
            "source",
            "request_id",
            "transcript_id",
            "sentence_index",
            "chars",
        }
    ] * 2
    body = "\n".join(journal.lines)
    assert "First sentence here" not in body
    assert "Second sentence here" not in body
    assert "hello" not in body
    await loop.close()


async def test_stream_failure_before_first_sentence_falls_back_to_full_reply(
    journal: RecordingJournal,
) -> None:
    agent = ScriptedAgent(
        [("error", RuntimeError("stream unsupported"))],
        fallback_reply="Full reply one. Full reply two. Full reply three.",
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["Full reply one.", "Full reply two."]
    [failed] = [row for row in journal.events("agent_failed") if row.get("detail") == "stream"]
    assert failed["request_id"] == "t1:1"
    assert journal.events("agent_finished") != []
    assert len(agent.ainvoke_calls) == 1
    await loop.close()


async def test_stream_failure_after_first_sentence_propagates_without_duplicate(
    journal: RecordingJournal,
) -> None:
    agent = ScriptedAgent(
        [("chunk", "First done. ", "model"), ("error", RuntimeError("mid stream"))]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    with pytest.raises(ConversationError, match="conversation failed"):
        await loop.wait_idle()
    assert audio.spoken == ["First done."]
    assert journal.events("agent_finished") == []
    assert agent.ainvoke_calls == []
    await loop.close()


async def test_streamed_speak_tags_each_sentence_with_its_index(journal: RecordingJournal) -> None:
    agent = ScriptedAgent([("chunk", "First sentence here. Second sentence here.", "model")])
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["First sentence here.", "Second sentence here."]
    assert audio.indices == [0, 1]
    await loop.close()


async def test_stale_drop_terminates_turn_with_agent_cancelled(journal: RecordingJournal) -> None:
    ready = asyncio.Event()
    release = asyncio.Event()
    agent = ScriptedAgent(
        {
            "t1": [
                ("chunk", "First done. ", "model"),
                ("gate", ready, release),
                ("chunk", "Second part here. ", "model"),
            ],
            "t2": [("chunk", "New answer.", "model")],
        }
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    await loop.handle_transcript(event("t2", "second"))
    release.set()
    await loop.wait_idle()
    [dropped] = journal.events("sentence_dropped")
    assert dropped["request_id"] == "t1:1"
    cancelled = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert len(cancelled) == 1
    assert "duration_ms" in cancelled[0]
    finished = [row for row in journal.events("agent_finished") if row["request_id"] == "t1:1"]
    assert finished == []
    await loop.close()


async def test_stopped_drop_terminates_turn_with_agent_cancelled(journal: RecordingJournal) -> None:
    ready = asyncio.Event()
    release = asyncio.Event()
    agent = ScriptedAgent(
        [
            ("chunk", "First done. ", "model"),
            ("gate", ready, release),
            ("chunk", "Second part here. ", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal, barge_in=True)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    audio.speaking = True
    await loop.handle_interim("wait just a moment")
    release.set()
    await loop.wait_idle()
    [dropped] = journal.events("sentence_dropped")
    assert dropped["detail"] == "stopped"
    cancelled = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert len(cancelled) == 1
    await loop.close()


def test_sentence_streaming_default_follows_settings_flag() -> None:
    import inspect

    from fakeyou.config.settings import DEFAULTS

    assert DEFAULTS.conversation.sentence_streaming is False
    params = inspect.signature(LoopGeneration).parameters
    assert params["sentence_streaming"].default == DEFAULTS.conversation.sentence_streaming


async def test_streamed_speak_passes_transcript_id_to_session(journal: RecordingJournal) -> None:
    agent = ScriptedAgent(
        [
            ("chunk", "First sent", "model"),
            ("chunk", "ence here. Second sent", "model"),
            ("chunk", "ence here.", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["First sentence here.", "Second sentence here."]
    assert audio.transcripts == ["t1", "t1"]
    await loop.close()
