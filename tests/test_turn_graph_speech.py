from collections.abc import AsyncIterator, Sequence
from typing import Any

from fakeyou.conversation.turn_graph import (
    SpeechInput,
    SpeechRuntime,
    build_speech_graph,
)


class FakeResponder:
    def __init__(self, sentences: Sequence[str], fail_after: int | None = None) -> None:
        self._sentences = tuple(sentences)
        self._fail_after = fail_after
        self.transcripts: list[str] = []

    async def stream_reply(self, transcript: str) -> AsyncIterator[str]:
        self.transcripts.append(transcript)
        for index, sentence in enumerate(self._sentences):
            if self._fail_after is not None and index == self._fail_after:
                raise RuntimeError("generator broke mid-reply")
            yield sentence
        if self._fail_after is not None and self._fail_after >= len(self._sentences):
            raise RuntimeError("generator broke mid-reply")


class FakeSink:
    def __init__(self) -> None:
        self.sentences: list[str] = []

    async def say(self, sentence: str) -> None:
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
