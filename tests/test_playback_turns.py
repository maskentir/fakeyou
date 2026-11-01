import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import override

from fakeyou.audio.playback import FRAME_BYTES, PlaybackEngine, PlaybackEvent
from fakeyou.observability.provider_faults import FaultEvidence, ProviderFault
from fakeyou.speech.contract import SpeechSegment, SpeechTurn, WordTiming
from fakeyou.speech.fallback import SpeechWithFallback
from tests._audio_support import FakeWriter


class RecordingSegment:
    def __init__(self, pcm: bytes) -> None:
        self._chunks = iter((pcm,))

    @property
    def words(self) -> Sequence[WordTiming]:
        return ()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        return None


class RecordingTurn:
    def __init__(self, voice: "RecordingVoice", transcript_id: str | None) -> None:
        self._voice = voice
        self.transcript_id = transcript_id
        self.spoken: list[tuple[str, int | None]] = []
        self.ended: str | None = None
        self.done = asyncio.Event()

    def speak(self, text: str, *, sentence_index: int | None = None) -> SpeechSegment:
        self.spoken.append((text, sentence_index))
        return RecordingSegment(b"\x01\x00" * (FRAME_BYTES // 2))

    async def finish(self) -> None:
        self.ended = "finished"
        self._voice.order.append(f"finish:{self.transcript_id}")
        self.done.set()

    async def cancel(self) -> None:
        self.ended = "cancelled"
        self._voice.order.append(f"cancel:{self.transcript_id}")
        self.done.set()


class RecordingVoice:
    def __init__(self) -> None:
        self.turns: list[RecordingTurn] = []
        self.order: list[str] = []
        self.closed = False

    def open_turn(
        self, *, transcript_id: str | None = None, language: str | None = None
    ) -> SpeechTurn:
        del language
        turn = RecordingTurn(self, transcript_id)
        self.turns.append(turn)
        return turn

    async def aclose(self) -> None:
        self.closed = True
        self.order.append("aclose")


def engine_for(voice: RecordingVoice) -> PlaybackEngine:
    events: list[PlaybackEvent] = []
    return PlaybackEngine(voice, FakeWriter(), on_event=events.append)


async def test_the_sentences_of_one_reply_share_one_turn() -> None:
    voice = RecordingVoice()
    engine = engine_for(voice)

    await engine.speak("First.", sentence_index=0, transcript_id="t1")
    await engine.speak("Second.", sentence_index=1, transcript_id="t1")

    [turn] = voice.turns
    assert turn.spoken == [("First.", 0), ("Second.", 1)]
    assert turn.ended is None
    await engine.close()


async def test_a_new_reply_finishes_the_previous_turn() -> None:
    voice = RecordingVoice()
    engine = engine_for(voice)

    await engine.speak("First.", sentence_index=0, transcript_id="t1")
    await engine.speak("Next.", sentence_index=0, transcript_id="t2")
    await asyncio.wait_for(voice.turns[0].done.wait(), timeout=2)

    first, second = voice.turns
    assert (first.transcript_id, first.ended) == ("t1", "finished")
    assert (second.transcript_id, second.ended) == ("t2", None)
    await engine.close()


async def test_a_prepared_sentence_joins_the_open_turn() -> None:
    voice = RecordingVoice()
    engine = engine_for(voice)

    prepared = await engine.prepare("First.", sentence_index=0, transcript_id="t1")
    following = await engine.prepare("Second.", sentence_index=1, transcript_id="t1")
    assert prepared is not None
    assert following is not None
    await engine.speak_prepared(prepared)
    await engine.speak_prepared(following)

    [turn] = voice.turns
    assert turn.spoken == [("First.", 0), ("Second.", 1)]
    await engine.close()


async def test_stop_cancels_the_open_turn() -> None:
    voice = RecordingVoice()
    engine = engine_for(voice)
    await engine.speak("First.", sentence_index=0, transcript_id="t1")

    await engine.stop()

    [turn] = voice.turns
    assert turn.ended == "cancelled"
    await engine.close()


async def test_close_cancels_the_open_turn_before_closing_the_voice() -> None:
    voice = RecordingVoice()
    engine = engine_for(voice)
    await engine.speak("First.", sentence_index=0, transcript_id="t1")

    await engine.close()

    [turn] = voice.turns
    assert turn.ended == "cancelled"
    assert voice.order == ["cancel:t1", "aclose"]


async def test_an_utterance_without_a_reply_gets_its_own_turn() -> None:
    voice = RecordingVoice()
    engine = engine_for(voice)

    await engine.speak("Hello everyone.")
    await engine.speak("Hello again.")
    await asyncio.wait_for(voice.turns[1].done.wait(), timeout=2)

    first, second = voice.turns
    assert (first.spoken, second.spoken) == ([("Hello everyone.", None)], [("Hello again.", None)])
    assert first.ended == second.ended == "finished"
    await engine.close()


async def test_a_cue_renders_on_its_own_turn_and_leaves_the_reply_turn_open() -> None:
    voice = RecordingVoice()
    engine = engine_for(voice)
    await engine.speak("First.", sentence_index=0, transcript_id="t1")

    await engine.prepare_cue("Hmm.")
    await engine.speak("Second.", sentence_index=1, transcript_id="t1")

    reply, *cues = voice.turns
    assert reply.spoken == [("First.", 0), ("Second.", 1)]
    assert reply.ended is None
    assert cues
    assert all(cue.spoken == [("Hmm.", None)] for cue in cues)
    assert all(cue.ended == "finished" for cue in cues)
    await engine.close()


async def test_an_utterance_without_a_reply_leaves_the_reply_turn_open() -> None:
    voice = RecordingVoice()
    engine = engine_for(voice)
    await engine.speak("First.", sentence_index=0, transcript_id="t1")

    await engine.speak("Hello everyone.")
    await engine.speak("Second.", sentence_index=1, transcript_id="t1")

    reply, greeting = voice.turns
    assert reply.spoken == [("First.", 0), ("Second.", 1)]
    assert reply.ended is None
    await asyncio.wait_for(greeting.done.wait(), timeout=2)
    await engine.close()


async def test_a_reply_prepared_ahead_moves_to_the_fallback_in_one_voice() -> None:
    primary = FailingVoice()
    fallback = RecordingVoice()
    engine = PlaybackEngine(SpeechWithFallback(primary, fallback), FakeWriter())

    first = await engine.prepare("First.", sentence_index=0, transcript_id="t1")
    second = await engine.prepare("Second.", sentence_index=1, transcript_id="t1")
    assert first is not None
    assert second is not None
    await engine.speak_prepared(first)
    await engine.speak_prepared(second)

    [turn] = fallback.turns
    assert turn.spoken == [("First.", 0), ("Second.", 1)]
    await engine.close()


class FailingSegment(RecordingSegment):
    @override
    async def __anext__(self) -> bytes:
        raise ProviderFault("tts", "unavailable", evidence=FaultEvidence(status=503))


class FailingTurn(RecordingTurn):
    @override
    def speak(self, text: str, *, sentence_index: int | None = None) -> SpeechSegment:
        self.spoken.append((text, sentence_index))
        return FailingSegment(b"")


class FailingVoice(RecordingVoice):
    @override
    def open_turn(
        self, *, transcript_id: str | None = None, language: str | None = None
    ) -> SpeechTurn:
        del language
        turn = FailingTurn(self, transcript_id)
        self.turns.append(turn)
        return turn
