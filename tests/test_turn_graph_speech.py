from collections.abc import AsyncIterator, Sequence
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_graph import (
    SpeechInput,
    SpeechRuntime,
    build_speech_graph,
)
from fakeyou.db.checkpoint import checkpoint_serializer


class FakeResponder:
    def __init__(self, sentences: Sequence[str], fail_after: int | None = None) -> None:
        self._sentences = tuple(sentences)
        self._fail_after = fail_after
        self.transcripts: list[str] = []

    async def stream_reply(self, transcript: str) -> AsyncIterator[SpokenChunk]:
        self.transcripts.append(transcript)
        for index, sentence in enumerate(self._sentences):
            if self._fail_after is not None and index == self._fail_after:
                raise RuntimeError("generator broke mid-reply")
            yield SpokenChunk(text=sentence, language=DEFAULTS.speech.language)
        if self._fail_after is not None and self._fail_after >= len(self._sentences):
            raise RuntimeError("generator broke mid-reply")


class FakeSink:
    def __init__(self) -> None:
        self.sentences: list[str] = []

    async def say(self, sentence: str, language: str) -> None:
        del language
        self.sentences.append(sentence)


class FakeJournal:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.fields: list[dict[str, Any]] = []

    def record(self, event: str, *, source: str, **fields: Any) -> None:
        self.names.append(event)
        self.fields.append({"source": source, **fields})


def speech_input(
    transcript: str = "twin, what is the plan?",
    speaking_generation: int = 4,
) -> SpeechInput:
    return SpeechInput(transcript=transcript, speaking_generation=speaking_generation)


async def test_streaming_respond_speaks_sentences_in_generator_order() -> None:
    responder = FakeResponder(["First sentence.", "Second sentence.", "Third sentence."])
    sink = FakeSink()
    graph = build_speech_graph()

    out = await graph.ainvoke(
        speech_input(),
        context=SpeechRuntime(responder=responder, sink=sink, journal=FakeJournal()),
    )

    assert sink.sentences == ["First sentence.", "Second sentence.", "Third sentence."]
    assert out["failure"] is None


async def test_speaking_generation_is_written_only_by_the_speak_node() -> None:
    graph = build_speech_graph()
    writers: list[str] = []

    async for chunk in graph.astream(
        speech_input(),
        context=SpeechRuntime(
            responder=FakeResponder(["Fresh one."]), sink=FakeSink(), journal=FakeJournal()
        ),
        stream_mode="updates",
    ):
        writers.extend(node for node, update in chunk.items() if "speaking_generation" in update)

    assert writers == ["speak"]


async def test_speaking_generation_advances_once_per_speech() -> None:
    graph = build_speech_graph()

    out = await graph.ainvoke(
        speech_input(speaking_generation=4),
        context=SpeechRuntime(
            responder=FakeResponder(["Fresh one."]),
            sink=FakeSink(),
            journal=FakeJournal(),
        ),
    )

    assert out["speaking_generation"] == 5


async def test_generator_failure_mid_reply_routes_to_a_named_failure_not_silence() -> None:
    sink = FakeSink()
    journal = FakeJournal()
    graph = build_speech_graph()

    out = await graph.ainvoke(
        speech_input(),
        context=SpeechRuntime(
            responder=FakeResponder(["First sentence.", "Second sentence."], fail_after=1),
            sink=sink,
            journal=journal,
        ),
    )

    assert sink.sentences == ["First sentence."]
    assert out["failure"] == "reply_failed"
    assert journal.names == ["turn_failed"]
    assert journal.fields == [{"source": "agent", "code": "reply_failed"}]


async def test_the_speech_state_stays_serializable_for_a_checkpoint() -> None:
    graph = build_speech_graph()
    serializer = checkpoint_serializer()

    out = await graph.ainvoke(
        speech_input(),
        context=SpeechRuntime(
            responder=FakeResponder(["First sentence.", "Second sentence."]),
            sink=FakeSink(),
            journal=FakeJournal(),
        ),
    )

    for _key, value in out.items():
        serializer.dumps_typed(value)


class _ParentState(TypedDict):
    transcript: str
    spoken: bool


async def test_the_speech_turn_survives_a_checkpointed_parent() -> None:
    speech = build_speech_graph()
    sink = FakeSink()

    async def _speak(state: _ParentState) -> dict[str, Any]:
        await speech.ainvoke(
            SpeechInput(transcript=state["transcript"], speaking_generation=0),
            context=SpeechRuntime(
                responder=FakeResponder(["Hello there."]), sink=sink, journal=FakeJournal()
            ),
        )
        return {"spoken": True}

    builder = StateGraph(_ParentState)
    builder.add_node("speak", _speak)
    builder.add_edge(START, "speak")
    builder.add_edge("speak", END)
    graph = builder.compile(checkpointer=InMemorySaver())

    out = await graph.ainvoke(
        {"transcript": "twin, are you there?", "spoken": False},
        {"configurable": {"thread_id": "t1:m1"}},
    )

    assert out["spoken"] is True
    assert sink.sentences == ["Hello there."]
