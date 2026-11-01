import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import override

from fakeyou.audio.playback import FRAME_BYTES, PlaybackEngine, PlaybackEvent, heard_text
from fakeyou.conversation.turn_judge import WindowSource
from fakeyou.memory.ingestion import meeting_windows
from fakeyou.orchestration.spoken_log import SpokenLog
from fakeyou.persistence.record import MeetingRecord, TwinSpeechEntry, load_meeting_record
from fakeyou.speech.contract import SpeechSegment, SpeechTurn, WordTiming
from tests._audio_support import FakeWriter
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal

FRAMES_BEFORE_STOP = 15
COUNTING = (
    WordTiming("One,", 0.0, 0.1),
    WordTiming("two,", 0.1, 0.25),
    WordTiming("three.", 0.25, 2.0),
)
Words = Sequence[WordTiming] | None


class StalledSegment:
    def __init__(self, words: Words, *, stalls: bool = True, flows: bool = False) -> None:
        self._words = words
        self._stalls = stalls
        self._flows = flows
        self._sent = 0
        self._stall = asyncio.Event()

    @property
    def words(self) -> Words:
        return self._words

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        if self._flows and self._sent >= FRAMES_BEFORE_STOP:
            await asyncio.sleep(0)
            return b"\x01\x00" * (FRAME_BYTES // 2)
        if self._sent < FRAMES_BEFORE_STOP:
            self._sent += 1
            return b"\x01\x00" * (FRAME_BYTES // 2)
        if self._stalls:
            await self._stall.wait()
        raise StopAsyncIteration

    async def aclose(self) -> None:
        return None


class StalledTurn:
    def __init__(self, words: Words, *, flows: bool) -> None:
        self._words = words
        self._flows = flows

    def speak(self, text: str, *, sentence_index: int | None = None) -> SpeechSegment:
        del sentence_index
        return StalledSegment(self._words, stalls=not text.startswith("Done"), flows=self._flows)

    async def finish(self) -> None:
        return None

    async def cancel(self) -> None:
        return None


class StalledVoice:
    def __init__(self, words: Words = COUNTING, *, flows: bool = False) -> None:
        self._words = words
        self._flows = flows

    def open_turn(
        self, *, transcript_id: str | None = None, language: str | None = None
    ) -> SpeechTurn:
        del transcript_id, language
        return StalledTurn(self._words, flows=self._flows)

    async def aclose(self) -> None:
        return None


class CountingWriter(FakeWriter):
    def __init__(self) -> None:
        super().__init__()
        self._wrote = asyncio.Event()

    @override
    async def write(self, pcm: bytes) -> None:
        await super().write(pcm)
        self._wrote.set()

    async def reached(self, frames: int) -> None:
        async with asyncio.timeout(2):
            while len(self.heard) < frames * FRAME_BYTES:
                self._wrote.clear()
                await self._wrote.wait()


def dropped(events: list[PlaybackEvent]) -> list[PlaybackEvent]:
    return [event for event in events if event.kind == "playback_dropped"]


async def test_a_stopped_sentence_reports_only_the_words_already_played() -> None:
    events: list[PlaybackEvent] = []
    writer = CountingWriter()
    engine = PlaybackEngine(StalledVoice(), writer, on_event=events.append)
    speaking = asyncio.create_task(
        engine.speak("One, two, three.", sentence_index=0, transcript_id="t1")
    )
    await writer.reached(FRAMES_BEFORE_STOP)

    await engine.stop()
    await speaking

    [stopped] = dropped(events)
    assert (stopped.detail, stopped.heard) == ("interrupted", "One, two,")
    await engine.close()


async def test_a_prepared_sentence_reports_the_words_already_played() -> None:
    events: list[PlaybackEvent] = []
    writer = CountingWriter()
    engine = PlaybackEngine(StalledVoice(), writer, on_event=events.append)
    prepared = await engine.prepare("One, two, three.", sentence_index=0, transcript_id="t1")
    assert prepared is not None
    speaking = asyncio.create_task(engine.speak_prepared(prepared))
    await writer.reached(FRAMES_BEFORE_STOP)

    await engine.stop()
    await speaking

    [stopped] = dropped(events)
    assert stopped.heard == "One, two,"
    await engine.close()


async def test_a_voice_without_word_timings_cannot_say_what_was_heard() -> None:
    events: list[PlaybackEvent] = []
    writer = CountingWriter()
    engine = PlaybackEngine(StalledVoice(words=None), writer, on_event=events.append)
    speaking = asyncio.create_task(engine.speak("One, two, three.", transcript_id="t1"))
    await writer.reached(FRAMES_BEFORE_STOP)

    await engine.stop()
    await speaking

    [stopped] = dropped(events)
    assert stopped.heard is None
    await engine.close()


async def test_a_cut_before_any_word_timing_arrived_heard_no_word() -> None:
    events: list[PlaybackEvent] = []
    writer = CountingWriter()
    engine = PlaybackEngine(StalledVoice(words=()), writer, on_event=events.append)
    speaking = asyncio.create_task(engine.speak("One, two, three.", transcript_id="t1"))
    await writer.reached(FRAMES_BEFORE_STOP)

    await engine.stop()
    await speaking

    [stopped] = dropped(events)
    assert stopped.heard == ""
    await engine.close()


async def test_a_sentence_still_streaming_when_stopped_reports_the_words_played() -> None:
    events: list[PlaybackEvent] = []
    writer = CountingWriter()
    engine = PlaybackEngine(StalledVoice(flows=True), writer, on_event=events.append)
    speaking = asyncio.create_task(engine.speak("One, two, three.", transcript_id="t1"))
    await writer.reached(FRAMES_BEFORE_STOP)

    await engine.stop()
    await speaking

    [stopped] = dropped(events)
    assert stopped.heard == "One, two,"
    await engine.close()


async def test_a_queued_sentence_dropped_before_playing_was_not_heard() -> None:
    events: list[PlaybackEvent] = []
    writer = CountingWriter()
    engine = PlaybackEngine(StalledVoice(), writer, on_event=events.append)
    first = asyncio.create_task(engine.speak("One, two, three.", transcript_id="t1"))
    await writer.reached(FRAMES_BEFORE_STOP)
    second = asyncio.create_task(engine.speak("Four.", sentence_index=1, transcript_id="t1"))
    await asyncio.sleep(0)

    await engine.stop()
    await asyncio.gather(first, second)

    assert {stopped.text: stopped.heard for stopped in dropped(events)} == {
        "One, two, three.": "One, two,",
        "Four.": "",
    }
    await engine.close()


async def test_each_sentence_counts_only_its_own_audio() -> None:
    events: list[PlaybackEvent] = []
    writer = CountingWriter()
    engine = PlaybackEngine(StalledVoice(), writer, on_event=events.append)
    await engine.speak("Done in full.", sentence_index=0, transcript_id="t1")
    speaking = asyncio.create_task(
        engine.speak("One, two, three.", sentence_index=1, transcript_id="t1")
    )
    await writer.reached(2 * FRAMES_BEFORE_STOP)

    await engine.stop()
    await speaking

    [stopped] = dropped(events)
    assert stopped.heard == "One, two,"
    await engine.close()


def test_a_word_counts_as_heard_once_it_has_fully_played() -> None:
    assert heard_text(COUNTING, 0.25) == "One, two,"
    assert heard_text(COUNTING, 0.24) == "One,"
    assert heard_text(COUNTING, 0.0) == ""
    assert heard_text((), 0.3) == ""
    assert heard_text(None, 0.3) is None


def test_the_heard_text_is_what_the_voice_said_not_a_count_of_the_written_words() -> None:
    said = (
        WordTiming("Meet", 0.0, 0.2),
        WordTiming("at", 0.2, 0.3),
        WordTiming("three", 0.3, 0.6),
        WordTiming("thirty", 0.6, 0.9),
        WordTiming("today.", 0.9, 1.3),
    )

    assert heard_text(said, 1.0) == "Meet at three thirty"


def logged(tmp_path: Path) -> tuple[SpokenLog, RecordingJournal, MeetingRecord]:
    journal = RecordingJournal()
    record = MeetingRecord(tmp_path / "record.jsonl")
    return SpokenLog(journal, record), journal, record


def started(text: str) -> PlaybackEvent:
    return PlaybackEvent("playback_started", text)


def cut(text: str, heard: str | None) -> PlaybackEvent:
    return PlaybackEvent("playback_dropped", text, "interrupted", heard=heard)


def twin_lines(record: MeetingRecord) -> list[tuple[str, bool, str | None]]:
    return [
        (entry.text, entry.interrupted, entry.planned)
        for entry in record.entries()
        if isinstance(entry, TwinSpeechEntry)
    ]


def test_an_interrupted_sentence_is_recorded_as_far_as_it_was_heard(tmp_path: Path) -> None:
    log, journal, record = logged(tmp_path)

    log.observe(started("One, two, three."))
    log.observe(cut("One, two, three.", "One, two,"))

    assert twin_lines(record) == [("One, two,", True, "One, two, three.")]
    assert journal.events("playback_dropped")[0]["heard_words"] == 2
    record.close()


def test_a_sentence_cut_without_word_timings_keeps_its_whole_text(tmp_path: Path) -> None:
    log, journal, record = logged(tmp_path)

    log.observe(started("One, two, three."))
    log.observe(cut("One, two, three.", None))

    assert twin_lines(record) == [("One, two, three.", True, None)]
    assert "heard_words" not in journal.events("playback_dropped")[0]
    record.close()


def test_a_sentence_heard_to_the_end_needs_no_plan(tmp_path: Path) -> None:
    log, _, record = logged(tmp_path)

    log.observe(started("One,  two."))
    log.observe(cut("One,  two.", "One, two."))

    assert twin_lines(record) == [("One,  two.", True, None)]
    record.close()


def test_a_sentence_cut_before_its_first_word_keeps_only_the_plan(tmp_path: Path) -> None:
    log, journal, record = logged(tmp_path)

    log.observe(started("One, two."))
    log.observe(cut("One, two.", ""))

    assert twin_lines(record) == [("", True, "One, two.")]
    assert journal.events("playback_dropped")[0]["heard_words"] == 0
    record.close()


def test_the_planned_sentence_survives_a_reload(tmp_path: Path) -> None:
    log, _, record = logged(tmp_path)
    log.observe(started("One, two, three."))
    log.observe(cut("One, two, three.", "One, two,"))
    record.close()

    [entry] = load_meeting_record(tmp_path / "record.jsonl")

    assert isinstance(entry, TwinSpeechEntry)
    assert (entry.text, entry.planned) == ("One, two,", "One, two, three.")


def test_a_sentence_never_heard_stays_out_of_the_judge_window(tmp_path: Path) -> None:
    record = MeetingRecord(tmp_path / "record.jsonl")
    record.add_twin_speech("", interrupted=True, planned="One, two.")

    window = WindowSource(record, window_lines=8).for_interim("still talking", twin_speaking=True)

    assert window.earlier == ()
    record.close()


def test_a_sentence_never_heard_stays_out_of_memory(tmp_path: Path) -> None:
    record = MeetingRecord(tmp_path / "record.jsonl")
    record.add_transcript(event("t1", "Can you hear me?"))
    record.add_twin_speech("", interrupted=True, planned="One, two.")

    windows = meeting_windows(record.entries())

    assert [utterance.text for window in windows for utterance in window] == ["Can you hear me?"]
    record.close()
