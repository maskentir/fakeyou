from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.conversation import (
    _filter_piece,
    _SentenceStream,
    _StreamPhase,
    cap_spoken_reply,
)
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.sentence_stream import (
    SentenceStream,
    shape_sentences,
)
from fakeyou.conversation.turn_graph import (
    SentenceLimits,
    SpeechRuntime,
    build_speech_graph,
)
from tests._model_responder import model_text

MODEL_NODE = {"langgraph_node": "model"}


async def _chunks(pieces: Sequence[str]) -> AsyncIterator[SpokenChunk]:
    for piece in pieces:
        yield SpokenChunk(text=piece, language=DEFAULTS.speech.language)


async def _shaped(pieces: Sequence[str], max_sentences: int, language: str = "en") -> list[str]:
    return [
        sentence.text
        async for sentence in shape_sentences(
            _chunks(pieces), max_sentences=max_sentences, language=language
        )
    ]


def _loop_pump(steps: Sequence[str | None], max_sentences: int, language: str = "en") -> list[str]:
    state = _SentenceStream("request", None, max_sentences, language, None)
    phase = _StreamPhase(lambda: None, None)
    snapshot = phase.boundary
    sentences: list[str] = []
    for step in steps:
        if step is None:
            phase.boundary += 1
            continue
        piece, snapshot, consumable = _filter_piece(
            AIMessage(content=step), MODEL_NODE, state, phase, snapshot
        )
        if not consumable:
            continue
        sentences.extend(state.take(piece))
        if state.check_cap():
            return sentences
    tail = state.flush()
    if tail is not None:
        sentences.append(tail)
    return sentences


CAP_CASES: list[tuple[list[str], int]] = [
    (["One. Two. Three. Four."], 2),
    (["One. Two. ", "Three. ", "Four."], 2),
    (["One.", " Two.", "   ", "Three."], 2),
    (["One. Two. "], 2),
    (["One. Two. Three"], 1),
    (["One. Two", " three. Four. Five."], 3),
    (["One.", "Two.", "Three."], 5),
]


@pytest.mark.parametrize(("pieces", "max_sentences"), CAP_CASES)
async def test_streamed_sentences_match_the_loop_under_the_cap(
    pieces: list[str], max_sentences: int
) -> None:
    assert await _shaped(pieces, max_sentences) == _loop_pump(pieces, max_sentences)


async def test_cap_stops_emission_at_the_limit_and_drops_the_rest() -> None:
    assert await _shaped(["One. Two. Three. Four."], 2) == ["One.", "Two."]


async def test_cap_still_waits_for_text_after_the_limit_before_stopping() -> None:
    assert await _shaped(["One.", " Two.", "   ", "Three."], 2) == ["One.", "Two."]


def test_sentence_stream_reports_the_cap_only_when_text_follows_the_limit() -> None:
    stream = SentenceStream(max_sentences=1, language="en")

    assert stream.take("One. ") == ["One."]
    assert stream.check_cap() is False
    assert stream.take("   ") == []
    assert stream.check_cap() is False
    assert stream.take("Two.") == []
    assert stream.check_cap() is True
    assert stream.flush() is None


async def test_trailing_fragment_is_emitted_only_after_the_stream_ends() -> None:
    events: list[str] = []

    async def pieces() -> AsyncIterator[SpokenChunk]:
        yield SpokenChunk(text="One. Two", language=DEFAULTS.speech.language)
        events.append("end")

    async for sentence in shape_sentences(pieces(), max_sentences=5, language="en"):
        events.append(sentence.text)

    assert events == ["One.", "end", "Two"]


def test_sentence_stream_holds_an_unterminated_fragment_until_flush() -> None:
    stream = SentenceStream(max_sentences=5, language="en")

    assert stream.take("Half a thought") == []
    assert stream.flush() == "Half a thought"


def test_flush_emits_nothing_when_only_whitespace_remains() -> None:
    stream = SentenceStream(max_sentences=5, language="en")

    assert stream.take("One.   ") == ["One."]
    assert stream.flush() is None


def test_flush_emits_nothing_once_the_cap_has_been_reached() -> None:
    stream = SentenceStream(max_sentences=2, language="en")

    assert stream.take("One. Two. ") == ["One.", "Two."]
    assert stream.check_cap() is False
    assert stream.flush() is None


async def test_full_reply_without_trailing_fragment_matches_the_loop() -> None:
    pieces = ["One. Two. ", "Three."]

    assert await _shaped(pieces, 5) == _loop_pump(pieces, 5) == ["One.", "Two.", "Three."]


def test_tool_boundary_discards_text_buffered_before_the_boundary() -> None:
    steps: list[str | None] = ["Half thought", None, "Fresh. "]

    stream = SentenceStream(max_sentences=5, language="en")
    assert stream.take("Half thought") == []
    stream.observe_boundary(1)
    assert stream.take("Fresh. ") == ["Fresh."]
    assert _loop_pump(steps, 5) == ["Fresh."]


FILTER_CASES: list[tuple[str, Any, bool]] = [
    ("Hi there.", {"langgraph_node": "model"}, False),
    ("Hi there.", {"langgraph_node": "tools"}, False),
    ("Hi there.", None, False),
    ("Hi there.", ["model"], False),
    ("", {"langgraph_node": "model"}, False),
    ("Hi there.", {"langgraph_node": "model"}, True),
]


@pytest.mark.parametrize(("text", "metadata", "pending"), FILTER_CASES)
def test_model_text_matches_the_loop_filter_for_each_chunk(
    text: str, metadata: Any, *, pending: bool
) -> None:
    state = _SentenceStream("request", None, 2, "en", None)
    phase = _StreamPhase(lambda: None, None)
    phase.pending = 1 if pending else 0

    loop_piece, _, _ = _filter_piece(AIMessage(content=text), metadata, state, phase, 0)

    assert model_text(AIMessage(content=text), metadata, tool_pending=pending) == loop_piece


def test_model_text_drops_non_message_chunks_as_the_loop_does() -> None:
    state = _SentenceStream("request", None, 2, "en", None)
    phase = _StreamPhase(lambda: None, None)

    loop_piece, _, _ = _filter_piece("raw string", MODEL_NODE, state, phase, 0)

    assert model_text("raw string", MODEL_NODE, tool_pending=False) == loop_piece == ""


def test_model_text_keeps_any_base_message_from_the_model_node_as_the_loop_does() -> None:
    state = _SentenceStream("request", None, 2, "en", None)
    phase = _StreamPhase(lambda: None, None)

    loop_piece, _, _ = _filter_piece(HumanMessage(content="Echo."), MODEL_NODE, state, phase, 0)

    assert model_text(HumanMessage(content="Echo."), MODEL_NODE, tool_pending=False) == loop_piece
    assert loop_piece == "Echo."


def test_model_text_keeps_text_exactly_as_produced_without_rewriting() -> None:
    message = AIMessage(content="  spaced out  ")

    assert model_text(message, MODEL_NODE, tool_pending=False) == "  spaced out  "


def test_cap_reply_matches_the_loop_whole_reply_cap() -> None:
    reply = "One. Two. Three. Four."

    assert cap_spoken_reply(reply, 2, "en") == "One. Two."


def test_cap_reply_keeps_a_blank_reply_unchanged() -> None:
    assert cap_spoken_reply("   ", 2, "en") == "   "


def test_cap_reply_rejects_a_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="max_sentences must be positive"):
        cap_spoken_reply("One.", 0, "en")


class _CappingResponder:
    async def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        for piece in ("One. Two. ", "Three. Four."):
            yield SpokenChunk(text=piece, language=DEFAULTS.speech.language)


class _RecordingSink:
    def __init__(self) -> None:
        self.sentences: list[str] = []

    async def say(self, sentence: str, language: str) -> None:
        del language
        self.sentences.append(sentence)


class _NamingJournal:
    def __init__(self) -> None:
        self.names: list[str] = []

    def record(self, event: str, *, source: str, **fields: Any) -> None:
        self.names.append(f"{source}:{event}:{sorted(fields)}")


async def test_speech_graph_speaks_only_capped_sentences_when_limits_are_set() -> None:
    sink = _RecordingSink()

    await build_speech_graph().ainvoke(
        {"transcript": "twin", "speaking_generation": 0},
        context=SpeechRuntime(
            responder=_CappingResponder(),
            sink=sink,
            journal=_NamingJournal(),
            sentences=SentenceLimits(max_sentences=2, language="en"),
        ),
    )

    assert sink.sentences == ["One.", "Two."]
