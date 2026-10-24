import asyncio
import json
import time
import uuid
from collections.abc import Iterable
from typing import Any, cast, override

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
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import (
    MeetingResponder,
    cap_spoken_reply,
    stream_sentences,
)
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.conversation.meeting_turns import (
    MeetingTurns,
    TurnGeneration,
    TurnInteraction,
    TurnTiming,
    build_meeting_turns,
)
from fakeyou.memory.memory import create_recall_error_middleware
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import FakeResponder, ToolCallingFakeModel, recall_call
from tests._conversation_support import AgentT, event
from tests._error_support import message_pattern
from tests._persistence_fakes import RecordingJournal
from tests._pregen_support import StreamingResponder


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
        del config
        del kwargs
        self.ainvoke_calls.append(payload)
        return {"messages": [AIMessage(content=self.fallback_reply)]}


def streamed_loop(
    agent: Any,
    audio: FakeReplyAudio,
    journal: RecordingJournal,
    language: str = "id",
    **kwargs: Any,
) -> MeetingTurns:
    responder = MeetingResponder(agent, NoHistory(), journal=journal, language=language)
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
    )
    interaction = TurnInteraction(
        **{key: kwargs.pop(key) for key in interaction_keys if key in kwargs}
    )
    timing = TurnTiming(**{key: kwargs.pop(key) for key in timing_keys if key in kwargs})
    generation = TurnGeneration(
        language=language,
        sentence_streaming=True,
        **{key: kwargs.pop(key) for key in generation_keys if key in kwargs},
    )
    assert not kwargs, kwargs
    loop = build_meeting_turns(
        responder,
        interaction=interaction,
        timing=timing,
        generation=generation,
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(audio)
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
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
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
    await loop.lifecycle.close()


@pytest.mark.parametrize("language", ["id", "en"])
async def test_streamed_reply_matches_cap_across_languages(
    journal: RecordingJournal, language: str
) -> None:
    agent = ScriptedAgent(
        [("chunk", "Morning update. All ", "model"), ("chunk", "systems ready.", "model")]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal, language=language)
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["Morning update.", "All systems ready."]
    assert " ".join(audio.spoken) == cap_spoken_reply(
        "Morning update. All systems ready.", language=language
    )
    await loop.lifecycle.close()


async def test_the_declared_language_reaches_the_speaker(journal: RecordingJournal) -> None:
    agent = ScriptedAgent(
        [("chunk", "intent: answer lang: en\nThe launch is on 14 October.", "model")]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal, language="id")
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["The launch is on 14 October."]
    assert audio.languages == ["en"]
    await loop.lifecycle.close()


async def test_a_silent_plan_speaks_nothing(journal: RecordingJournal) -> None:
    agent = ScriptedAgent(
        [
            ("chunk", "intent: sil", "model"),
            ("chunk", "ent\nI would have said something.", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == []
    await loop.lifecycle.close()


async def test_an_ack_plan_speaks_one_sentence(journal: RecordingJournal) -> None:
    agent = ScriptedAgent([("chunk", "intent: ack\nOkay. More to say.", "model")])
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["Okay."]
    await loop.lifecycle.close()


async def test_an_answer_plan_drops_the_closing_question(journal: RecordingJournal) -> None:
    agent = ScriptedAgent(
        [("chunk", "intent: answer\nThe launch is on 14 October. Anything else?", "model")]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal, language="en")
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["The launch is on 14 October."]
    assert "intent" not in " ".join(audio.spoken)
    await loop.lifecycle.close()


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
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["Real answer here.", "Done now."]
    body = "\n".join(journal.lines)
    assert "tool output secret" not in body
    assert "Real answer here" not in body
    await loop.lifecycle.close()


async def test_streaming_responder_with_flag_off_uses_single_speak(
    journal: RecordingJournal,
) -> None:
    agent = ScriptedAgent([], fallback_reply="One. Two. Three.")
    audio = FakeReplyAudio()
    responder = MeetingResponder(cast(AgentT, agent), NoHistory(), journal=journal)
    loop = build_meeting_turns(
        responder,
        generation=TurnGeneration(sentence_streaming=False),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["One. Two."]
    assert journal.events("answer_text_emitted") == []
    assert journal.events("answer_sentence_emitted") == []
    assert len(agent.ainvoke_calls) == 1
    assert agent.streams == []
    await loop.lifecycle.close()


async def test_flag_on_with_plain_responder_keeps_old_behavior() -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = build_meeting_turns(responder, generation=TurnGeneration(sentence_streaming=True))
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["hello"]
    await loop.lifecycle.close()


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
    await loop.ingress.receive_final(event("t1", "when is demo"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["The demo is on Friday.", "Doors open at nine."]
    body = "\n".join(journal.lines)
    assert "Checking memor" not in body
    assert "demo friday" not in body
    [started_tool] = journal.events("tool_started")
    assert (started_tool["tool"], started_tool["transcript_id"]) == ("recall_meeting_context", "t1")
    [finished_tool] = journal.events("tool_finished")
    assert finished_tool["transcript_id"] == "t1"
    await loop.lifecycle.close()


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
    await loop.ingress.receive_final(event("t1", "when is demo"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["The demo is on Friday."]
    order = [json.loads(line)["event"] for line in journal.lines]
    assert order.index("answer_text_emitted") > order.index("tool_finished")
    await loop.lifecycle.close()


async def test_streamed_reply_survives_recall_failure(journal: RecordingJournal) -> None:
    from langchain_core.tools import tool

    @tool(description="Search long-term meeting memory for relevant context.")
    async def recall_meeting_context(query: str) -> str:
        del query
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
    await loop.ingress.receive_final(event("t1", "when is demo"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["I do not know the answer.", "Sorry about that."]
    assert "Meeting memory is unavailable" not in " ".join(audio.spoken)
    [failed] = journal.events("tool_failed")
    assert (failed["tool"], failed["transcript_id"]) == ("recall_meeting_context", "t1")
    assert journal.events("agent_finished") != []
    await loop.lifecycle.close()


async def test_streamed_reply_uses_fallback_model(journal: RecordingJournal) -> None:
    from langchain.agents.middleware import ModelFallbackMiddleware

    class FailingChatModel(ToolCallingFakeModel):
        @override
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
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["Fallback answer here."]
    assert journal.events("agent_finished") != []
    await loop.lifecycle.close()


async def test_streamed_reply_caps_third_sentence_and_stops_stream(
    journal: RecordingJournal,
) -> None:
    agent = ScriptedAgent(
        [("chunk", "One. Two. Three.", "model"), ("chunk", " Four. Five.", "model")]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["One.", "Two."]
    assert " ".join(audio.spoken) == cap_spoken_reply("One. Two. Three. Four. Five.")
    [capped] = journal.events("reply_capped")
    assert capped["chars_in"] >= capped["chars_out"] > 0
    assert agent.delivered == 1
    assert agent.streams[0].aclosed
    await loop.lifecycle.close()


async def test_streamed_reply_without_remainder_records_no_cap(journal: RecordingJournal) -> None:
    agent = ScriptedAgent([("chunk", "One. Two.", "model")])
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["One.", "Two."]
    assert journal.events("reply_capped") == []
    await loop.lifecycle.close()


async def test_streamed_empty_reply_moves_to_waiting(journal: RecordingJournal) -> None:
    agent = ScriptedAgent([])
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == []
    assert loop.lifecycle.phase == "waiting"
    await loop.lifecycle.close()


def test_streaming_responder_rejects_non_positive_cap() -> None:
    with pytest.raises(ValueError, match="max_sentences"):
        MeetingResponder(cast(AgentT, ScriptedAgent([])), NoHistory(), max_sentences=0)


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
    await loop.ingress.receive_final(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    await loop.ingress.receive_final(event("t2", "second"))
    release.set()
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["First done.", "New answer."]
    [dropped] = journal.events("sentence_dropped")
    assert (dropped["request_id"], dropped["transcript_id"]) == ("t1:1", "t1")
    assert (dropped["sentence_index"], dropped["detail"]) == (1, "stale_generation")
    assert agent.streams[0].aclosed
    await loop.lifecycle.close()


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
    await loop.ingress.receive_final(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    audio.speaking = True
    await loop.ingress.handle_interim("wait just a moment")
    assert audio.stops == 1
    release.set()
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["First done."]
    [dropped] = journal.events("sentence_dropped")
    assert (dropped["transcript_id"], dropped["sentence_index"], dropped["detail"]) == (
        "t1",
        1,
        "stopped",
    )
    await loop.lifecycle.close()


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
    await loop.ingress.receive_final(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    await loop.lifecycle.close()
    assert audio.spoken == ["First done."]
    assert agent.streams[0].aclosed
    assert journal.events("agent_cancelled") != []


async def test_streamed_journal_events_carry_metadata_only(journal: RecordingJournal) -> None:
    agent = ScriptedAgent([("chunk", "First sentence here. Second sentence here.", "model")])
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
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
    await loop.lifecycle.close()


async def test_stream_failure_before_first_sentence_falls_back_to_full_reply(
    journal: RecordingJournal,
) -> None:
    agent = ScriptedAgent(
        [("error", RuntimeError("stream unsupported"))],
        fallback_reply="Full reply one. Full reply two. Full reply three.",
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["Full reply one.", "Full reply two."]
    [failed] = [row for row in journal.events("agent_failed") if row.get("detail") == "stream"]
    assert failed["request_id"] == "t1:1"
    assert journal.events("agent_finished") != []
    assert len(agent.ainvoke_calls) == 1
    await loop.lifecycle.close()


async def test_stream_failure_after_first_sentence_propagates_without_duplicate(
    journal: RecordingJournal,
) -> None:
    agent = ScriptedAgent(
        [("chunk", "First done. ", "model"), ("error", RuntimeError("mid stream"))]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    loop.lifecycle.raise_if_failed()
    assert audio.spoken.count("First done.") == 1
    assert len(audio.spoken) == 2
    assert journal.events("agent_finished") == []
    assert agent.ainvoke_calls == []
    await loop.lifecycle.close()


async def test_streamed_speak_tags_each_sentence_with_its_index(journal: RecordingJournal) -> None:
    agent = ScriptedAgent([("chunk", "First sentence here. Second sentence here.", "model")])
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["First sentence here.", "Second sentence here."]
    assert audio.indices == [0, 1]
    await loop.lifecycle.close()


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
    await loop.ingress.receive_final(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    await loop.ingress.receive_final(event("t2", "second"))
    release.set()
    await loop.lifecycle.wait_idle()
    [dropped] = journal.events("sentence_dropped")
    assert dropped["request_id"] == "t1:1"
    cancelled = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert len(cancelled) == 1
    assert "duration_ms" in cancelled[0]
    finished = [row for row in journal.events("agent_finished") if row["request_id"] == "t1:1"]
    assert finished == []
    await loop.lifecycle.close()


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
    await loop.ingress.receive_final(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    audio.speaking = True
    await loop.ingress.handle_interim("wait just a moment")
    release.set()
    await loop.lifecycle.wait_idle()
    [dropped] = journal.events("sentence_dropped")
    assert dropped["detail"] == "stopped"
    cancelled = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert len(cancelled) == 1
    await loop.lifecycle.close()


def test_sentence_streaming_default_follows_settings_flag() -> None:
    import inspect

    from fakeyou.config.settings import DEFAULTS

    assert DEFAULTS.conversation.sentence_streaming is True
    params = inspect.signature(TurnGeneration).parameters
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
    await loop.ingress.receive_final(event("t1", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["First sentence here.", "Second sentence here."]
    assert audio.transcripts == ["t1", "t1"]
    await loop.lifecycle.close()


async def test_streamed_reply_passes_domain_error_through_after_first_sentence(
    journal: RecordingJournal,
) -> None:
    agent = ScriptedAgent(
        {
            "t1": [
                ("chunk", "First sentence here. ", "model"),
                ("error", FakeYouError(ErrorCode.CONFIG_PERCEPTION_BASE_URL_REQUIRED)),
            ]
        }
    )
    responder = MeetingResponder(cast(AgentT, agent), NoHistory(), journal=journal)

    seen: list[str] = []

    async def _drain() -> None:
        async for sentence in responder.stream_sentences(
            [event("t1", "hi")], lambda: None, request_id="req-1"
        ):
            seen.append(sentence.text)

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.CONFIG_PERCEPTION_BASE_URL_REQUIRED),
    ) as caught:
        await _drain()

    assert caught.value.code is ErrorCode.CONFIG_PERCEPTION_BASE_URL_REQUIRED
    assert seen == ["First sentence here."]


async def test_stream_error_before_first_sentence_skips_recovery_for_domain_error(
    journal: RecordingJournal,
) -> None:
    agent = ScriptedAgent(
        {"t1": [("error", FakeYouError(ErrorCode.CONFIG_PERCEPTION_BASE_URL_REQUIRED))]}
    )
    responder = MeetingResponder(cast(AgentT, agent), NoHistory(), journal=journal)

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.CONFIG_PERCEPTION_BASE_URL_REQUIRED),
    ) as caught:
        async for _ in responder.stream_sentences(
            [event("t1", "hi")], lambda: None, request_id="req-1"
        ):
            pass

    assert caught.value.code is ErrorCode.CONFIG_PERCEPTION_BASE_URL_REQUIRED
    assert len(agent.streams) == 1


async def test_speaking_time_does_not_count_against_the_response_budget() -> None:
    class SlowReplyAudio(FakeReplyAudio):
        @override
        async def speak(
            self,
            text: str,
            *,
            sentence_index: int | None = None,
            transcript_id: str | None = None,
            language: str | None = None,
        ) -> None:
            await asyncio.sleep(0.2)
            await super().speak(
                text,
                sentence_index=sentence_index,
                transcript_id=transcript_id,
                language=language,
            )

    responder = StreamingResponder(["First sentence.", "Second sentence."])
    audio = SlowReplyAudio()
    loop = build_meeting_turns(
        responder,
        timing=TurnTiming(response_timeout_seconds=0.1),
        generation=TurnGeneration(sentence_streaming=True),
    )
    loop.lifecycle.bind(audio)

    await loop.ingress.receive_final(event("t1", "question"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 3)

    assert audio.spoken == ["First sentence.", "Second sentence."]
    loop.lifecycle.raise_if_failed()
    await loop.lifecycle.close()


class SlowTokenStream:
    def __init__(self, pieces: list[str], delay: float) -> None:
        self._pieces = list(pieces)
        self._delay = delay
        self._first = True

    def __aiter__(self) -> "SlowTokenStream":
        return self

    async def __anext__(self) -> tuple[AIMessageChunk, dict[str, str]]:
        if not self._pieces:
            raise StopAsyncIteration
        if not self._first:
            await asyncio.sleep(self._delay)
        self._first = False
        return AIMessageChunk(content=self._pieces.pop(0)), {"langgraph_node": "model"}


class SlowTokenAgent:
    def __init__(self, pieces: list[str], delay: float) -> None:
        self.pieces = pieces
        self.delay = delay

    def astream(
        self, payload: dict[str, Any], config: dict[str, Any] | None = None, **kwargs: Any
    ) -> SlowTokenStream:
        del payload, config, kwargs
        return SlowTokenStream(self.pieces, self.delay)

    async def ainvoke(
        self, payload: dict[str, Any], config: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        del payload, config, kwargs
        await asyncio.sleep(self.delay * (len(self.pieces) - 1))
        return {"messages": [AIMessage(content="".join(self.pieces))]}


class ToolInterleavedStream:
    def __init__(self, callbacks: Iterable[Any]) -> None:
        self._callbacks = list(callbacks)
        self._steps = ["tool_start", "during", "tool_end", "after"]
        self._run_id = uuid.uuid4()

    def __aiter__(self) -> "ToolInterleavedStream":
        return self

    async def __anext__(self) -> tuple[AIMessageChunk, dict[str, str]]:
        while self._steps:
            step = self._steps.pop(0)
            if step == "tool_start":
                for callback in self._callbacks:
                    await callback.on_tool_start({"name": "lookup"}, "", run_id=self._run_id)
            elif step == "tool_end":
                outcome = ToolMessage(content="found", tool_call_id="call-1")
                for callback in self._callbacks:
                    await callback.on_tool_end(outcome, run_id=self._run_id)
            elif step == "during":
                return AIMessageChunk(content="Still looking it up. "), {"langgraph_node": "model"}
            else:
                return AIMessageChunk(content="I have checked it."), {"langgraph_node": "model"}
        raise StopAsyncIteration


class ToolInterleavedAgent:
    def astream(
        self, payload: dict[str, Any], config: dict[str, Any] | None = None, **kwargs: Any
    ) -> ToolInterleavedStream:
        del payload, kwargs
        return ToolInterleavedStream((config or {}).get("callbacks", ()))


def stream_config() -> RunnableConfig:
    return {"configurable": {"thread_id": "thread"}, "recursion_limit": 30}


async def test_first_sentence_arrives_before_stream_ends() -> None:
    agent = SlowTokenAgent(["First sentence. ", "Second sentence."], delay=0.30)
    stream = stream_sentences(
        cast(AgentT, agent), [HumanMessage("hello")], stream_config(), max_sentences=6
    )
    start = time.perf_counter()
    first = await anext(stream)
    assert first.text == "First sentence."
    assert time.perf_counter() - start < 0.30
    assert [first.text] + [s.text async for s in stream] == [
        "First sentence.",
        "Second sentence.",
    ]


async def test_streamed_sentences_match_non_streaming_answer() -> None:
    agent = SlowTokenAgent(["First sentence. ", "Second sentence. ", "Third one."], delay=0.0)
    streamed = [
        s
        async for s in stream_sentences(
            cast(AgentT, agent), [HumanMessage("hello")], stream_config(), max_sentences=2
        )
    ]
    whole = await MeetingResponder(cast(AgentT, agent), NoHistory()).respond(
        [event("t1", "hello")], lambda: None
    )
    assert [chunk.text for chunk in streamed] == ["First sentence.", "Second sentence."]
    assert " ".join(chunk.text for chunk in streamed) == cap_spoken_reply(whole.text, 2)


async def test_text_during_tool_run_is_not_spoken() -> None:
    spoken = [
        s
        async for s in stream_sentences(
            cast(AgentT, ToolInterleavedAgent()),
            [HumanMessage("check it")],
            stream_config(),
            max_sentences=6,
        )
    ]
    assert [chunk.text for chunk in spoken] == ["I have checked it."]


async def test_streaming_first_sentence_beats_the_whole_turn_path() -> None:
    agent = SlowTokenAgent(["First sentence. ", "Second sentence. ", "Third one."], delay=0.10)
    start = time.perf_counter()
    whole = await MeetingResponder(cast(AgentT, agent), NoHistory()).respond(
        [event("t1", "hello")], lambda: None
    )
    whole_first_ms = (time.perf_counter() - start) * 1000
    stream = stream_sentences(
        cast(AgentT, agent), [HumanMessage("hello")], stream_config(), max_sentences=6
    )
    start = time.perf_counter()
    first = await anext(stream)
    streamed_first_ms = (time.perf_counter() - start) * 1000
    rest = [s async for s in stream]
    assert " ".join([first.text, *(chunk.text for chunk in rest)]) == cap_spoken_reply(
        whole.text, 6
    )
    assert streamed_first_ms < whole_first_ms
