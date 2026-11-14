import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import override

import pytest

from fakeyou.audio.playback import (
    BYTES_PER_SECOND,
    FRAME_BYTES,
    PlaybackEngine,
    PlaybackEvent,
)
from fakeyou.conversation.actor import ActorWiring, FloorActor
from fakeyou.conversation.turn_graph import build_turn_graph
from fakeyou.foundation.publication import (
    PublicationContext,
    PublicationDecision,
    PublicationValidity,
    SentenceBinding,
    sentence_digest,
)
from fakeyou.orchestration.spoken_log import SpokenLog
from fakeyou.persistence.record import MeetingRecord, TwinSpeechEntry
from fakeyou.speech.contract import PlaybackOutcome, PlaybackReceipt
from tests._audio_fakes import FakeReplyAudio
from tests._audio_support import FakeSynth, FakeWriter, build_engine
from tests._model_responder import ModelResponder
from tests._persistence_fakes import RecordingJournal
from tests.test_playback_cues import CUE_AUDIO
from tests.test_turn_graph_whole_adapter import FakeJournal, ToolPhaseState, unused_chunks

FRAME = b"\x01\x00" * (FRAME_BYTES // 2)
THREE_FRAMES = FRAME * 3


class StallingWriter(FakeWriter):
    def __init__(self, frames_before_stall: int) -> None:
        super().__init__()
        self._remaining = frames_before_stall
        self.stalled = asyncio.Event()

    @override
    async def write(self, pcm: bytes) -> None:
        if self._remaining == 0:
            self.stalled.set()
            self.release.clear()
        self._remaining -= 1
        try:
            await super().write(pcm)
        except asyncio.CancelledError:
            self.release.set()
            raise


@dataclass
class Playback:
    audio: bytes = THREE_FRAMES
    writer: FakeWriter = field(default_factory=FakeWriter)
    events: list[PlaybackEvent] = field(default_factory=list)
    receipts: list[PlaybackReceipt] = field(default_factory=list)
    engine: PlaybackEngine = field(init=False)

    def __post_init__(self) -> None:
        self.engine = build_engine(FakeSynth(audio=self.audio), self.writer, self.events)
        self.engine.observe_receipts(self.receipts.append)

    def gate(self, validity: PublicationValidity | None) -> None:
        async def decide(_binding: SentenceBinding) -> PublicationDecision:
            assert validity is not None
            return PublicationDecision(validity, "scripted", 1.0, "test")

        self.engine.configure_publication(
            PublicationContext(
                "meeting", "test", None if validity is None else decide, now=lambda: 1.0
            )
        )

    @property
    def outcomes(self) -> list[PlaybackOutcome]:
        return [item.outcome for item in self.receipts]

    @property
    def attempts(self) -> list[str]:
        return list(dict.fromkeys(item.attempt_id for item in self.receipts))

    @property
    def pcm_seconds(self) -> float:
        return len(self.writer.heard) / BYTES_PER_SECOND


def bound(text: str, publication_id: str = "pub-1") -> SentenceBinding:
    return SentenceBinding(
        publication_id,
        "meeting",
        "turn",
        "transcript",
        "request",
        0,
        sentence_digest(text),
        None,
        (),
        (),
        "no_visual",
    )


async def speak_bound(playback: Playback, text: str, publication_id: str = "pub-1") -> None:
    prepared = await playback.engine.prepare(text, sentence_index=0, transcript_id="transcript")
    assert prepared is not None
    playback.engine.bind_publication(prepared, bound(prepared.text, publication_id))
    await playback.engine.speak_prepared(prepared)


async def test_written_clip_reports_attempt_start_and_completion_once() -> None:
    playback = Playback()
    try:
        await playback.engine.speak("Hello there.", transcript_id="t1", sentence_index=4)
        assert playback.outcomes == ["attempted", "audio_started", "completed"]
        assert len(playback.attempts) == 1
        attempted, started, completed = playback.receipts
        assert attempted.local_audio_seconds == 0
        assert started.local_audio_seconds == pytest.approx(FRAME_BYTES / BYTES_PER_SECOND)
        assert completed.local_audio_seconds == pytest.approx(playback.pcm_seconds)
        assert completed.local_audio_seconds == pytest.approx(3 * started.local_audio_seconds)
        assert (completed.transcript_id, completed.sentence_index) == ("t1", 4)
        assert [item.local_audio_written for item in playback.receipts] == [False, True, True]
        assert [item.settled for item in playback.receipts] == [False, False, True]
    finally:
        await playback.engine.close()


async def test_receipts_ride_on_the_matching_playback_events() -> None:
    playback = Playback()
    try:
        await playback.engine.speak("Hello there.")
        assert [event.receipt for event in playback.events] == playback.receipts
        assert [event.kind for event in playback.events] == [
            "playback_started",
            "playback_audio_started",
            "playback_finished",
        ]
    finally:
        await playback.engine.close()


@pytest.mark.parametrize("validity", ["invalid", "unknown"])
async def test_gate_veto_before_first_pcm_is_prevented(validity: PublicationValidity) -> None:
    playback = Playback()
    playback.gate(validity)
    try:
        await speak_bound(playback, "The slide shows revenue.")
        assert playback.outcomes == ["attempted", "prevented"]
        assert playback.receipts[-1].publication_id == "pub-1"
        assert playback.receipts[-1].local_audio_seconds == 0
        assert playback.writer.written == []
    finally:
        await playback.engine.close()


async def test_unbound_direct_speech_under_a_context_is_prevented() -> None:
    playback = Playback()
    playback.gate(None)
    try:
        await playback.engine.speak("The slide shows revenue.")
        assert playback.outcomes == ["attempted", "prevented"]
        assert "unresolved" in playback.receipts[-1].detail
        assert playback.writer.written == []
    finally:
        await playback.engine.close()


async def test_declared_no_visual_speech_under_a_context_is_written() -> None:
    playback = Playback()
    playback.gate("invalid")
    try:
        await playback.engine.speak_no_visual("Hi everyone.")
        assert playback.outcomes == ["attempted", "audio_started", "completed"]
        assert playback.receipts[-1].publication_id is None
        assert playback.writer.written
    finally:
        await playback.engine.close()


async def test_nothing_to_say_is_dropped_before_audio() -> None:
    playback = Playback()
    try:
        await playback.engine.speak("...")
        assert await playback.engine.prepare("?!") is None
        assert playback.outcomes == ["dropped_before_audio", "dropped_before_audio"]
        assert len(playback.attempts) == 2
        assert {item.detail for item in playback.receipts} == {"nothing_to_say"}
        assert playback.writer.written == []
    finally:
        await playback.engine.close()


async def test_empty_clip_never_counts_as_completed_audio() -> None:
    playback = Playback(audio=b"")
    try:
        await playback.engine.speak("Hello there.")
        assert playback.outcomes == ["attempted", "dropped_before_audio"]
        assert playback.receipts[-1].local_audio_written is False
        assert playback.writer.written == []
    finally:
        await playback.engine.close()


async def test_cancellation_before_the_first_pcm_is_dropped_before_audio() -> None:
    playback = Playback(writer=StallingWriter(0))
    speaking = asyncio.create_task(playback.engine.speak("Hello there."))
    try:
        assert isinstance(playback.writer, StallingWriter)
        await playback.writer.stalled.wait()
        await playback.engine.stop()
        await speaking
        assert playback.outcomes == ["attempted", "dropped_before_audio"]
        assert playback.writer.cancelled == [FRAME]
        assert FRAME not in playback.writer.written
    finally:
        playback.writer.release.set()
        await playback.engine.close()


async def test_queued_clip_stopped_before_its_turn_is_dropped_before_audio() -> None:
    playback = Playback(writer=StallingWriter(1))
    first = asyncio.create_task(playback.engine.speak("First sentence."))
    try:
        assert isinstance(playback.writer, StallingWriter)
        await playback.writer.stalled.wait()
        second = asyncio.create_task(playback.engine.speak("Second sentence."))
        await asyncio.sleep(0)
        await playback.engine.stop()
        await asyncio.gather(first, second)
        by_text = {
            text: [item.outcome for item in playback.receipts if item.text == text]
            for text in ("First sentence.", "Second sentence.")
        }
        assert by_text["First sentence."] == [
            "attempted",
            "audio_started",
            "interrupted_after_audio",
        ]
        assert by_text["Second sentence."] == ["dropped_before_audio"]
    finally:
        playback.writer.release.set()
        await playback.engine.close()


async def test_barge_in_after_partial_pcm_reports_the_partial_duration() -> None:
    playback = Playback(writer=StallingWriter(1))
    speaking = asyncio.create_task(playback.engine.speak("A long sentence that gets cut."))
    try:
        assert isinstance(playback.writer, StallingWriter)
        await playback.writer.stalled.wait()
        await playback.engine.stop()
        await speaking
        assert playback.outcomes == ["attempted", "audio_started", "interrupted_after_audio"]
        assert len(playback.attempts) == 1
        interrupted = playback.receipts[-1]
        assert interrupted.settled
        assert interrupted.local_audio_seconds == pytest.approx(FRAME_BYTES / BYTES_PER_SECOND)
        assert interrupted.local_audio_seconds < len(THREE_FRAMES) / BYTES_PER_SECOND
        assert "completed" not in playback.outcomes
    finally:
        playback.writer.release.set()
        await playback.engine.close()


async def test_replayed_sentence_is_a_new_attempt_with_its_own_receipts() -> None:
    playback = Playback()
    playback.gate("valid")
    try:
        await speak_bound(playback, "Same sentence.", "pub-1")
        await speak_bound(playback, "Same sentence.", "pub-2")
        assert playback.outcomes == ["attempted", "audio_started", "completed"] * 2
        assert len(playback.attempts) == 2
        assert [item.publication_id for item in playback.receipts if item.settled] == [
            "pub-1",
            "pub-2",
        ]
    finally:
        await playback.engine.close()


async def test_prevented_then_replayed_attempt_is_judged_on_its_own_outcome() -> None:
    playback = Playback()
    playback.gate("invalid")
    try:
        await speak_bound(playback, "Same sentence.", "pub-1")
        playback.gate("valid")
        await speak_bound(playback, "Same sentence.", "pub-2")
        settled = {item.publication_id: item.outcome for item in playback.receipts if item.settled}
        assert settled == {"pub-1": "prevented", "pub-2": "completed"}
    finally:
        await playback.engine.close()


async def test_cue_receipts_are_marked_as_cues() -> None:
    playback = Playback(audio=CUE_AUDIO)
    try:
        await playback.engine.prepare_cue("Hmm…")
        assert await playback.engine.play_cue("Hmm…") is True
        assert playback.receipts
        assert all(item.cue for item in playback.receipts)
    finally:
        await playback.engine.close()


def actor_on(audio: FakeReplyAudio) -> tuple[FloorActor, list[PlaybackReceipt]]:
    actor = FloorActor(
        build_turn_graph(),
        ActorWiring(
            journal=FakeJournal(),
            responder=ModelResponder(
                chunks=unused_chunks, phase=ToolPhaseState(), max_sentences=3, language="en"
            ),
        ),
    )
    counted: list[PlaybackReceipt] = []
    actor.observe_spoken(counted.append)
    actor.bind(audio)
    return actor, counted


@pytest.mark.parametrize("outcome", ["attempted", "prevented", "dropped_before_audio"])
async def test_actor_counts_nothing_without_local_audio(outcome: PlaybackOutcome) -> None:
    audio = FakeReplyAudio()
    actor, counted = actor_on(audio)
    try:
        audio.deliver(PlaybackReceipt("a1", outcome, "Sentence."))
        assert counted == []
    finally:
        await actor.close()


async def test_actor_counts_one_attempt_once_across_repeated_receipts() -> None:
    audio = FakeReplyAudio()
    actor, counted = actor_on(audio)
    started = PlaybackReceipt("a1", "audio_started", "Sentence.", local_audio_seconds=0.02)
    try:
        for receipt in (
            PlaybackReceipt("a1", "attempted", "Sentence."),
            started,
            started,
            PlaybackReceipt("a1", "interrupted_after_audio", "Sentence.", local_audio_seconds=0.4),
            PlaybackReceipt("a1", "completed", "Sentence.", local_audio_seconds=1.0),
        ):
            audio.deliver(receipt)
        assert counted == [started]
    finally:
        await actor.close()


async def test_actor_counts_a_new_attempt_of_the_same_sentence_again() -> None:
    audio = FakeReplyAudio()
    actor, counted = actor_on(audio)
    try:
        audio.deliver(PlaybackReceipt("a1", "prevented", "Sentence."))
        audio.deliver(PlaybackReceipt("a2", "audio_started", "Sentence."))
        audio.deliver(PlaybackReceipt("a3", "audio_started", "Sentence."))
        assert [item.attempt_id for item in counted] == ["a2", "a3"]
    finally:
        await actor.close()


async def test_actor_counts_interrupted_audio_whose_start_was_never_reported() -> None:
    audio = FakeReplyAudio()
    actor, counted = actor_on(audio)
    try:
        audio.deliver(PlaybackReceipt("a1", "interrupted_after_audio", "Sentence."))
        assert len(counted) == 1
    finally:
        await actor.close()


async def test_actor_ignores_cue_audio_and_receipts_after_close() -> None:
    audio = FakeReplyAudio()
    actor, counted = actor_on(audio)
    audio.deliver(PlaybackReceipt("a1", "audio_started", "Hmm.", cue=True))
    await actor.close()
    audio.deliver(PlaybackReceipt("a2", "audio_started", "Sentence."))
    assert counted == []


async def test_rebinding_the_same_session_does_not_double_count() -> None:
    audio = FakeReplyAudio()
    actor, counted = actor_on(audio)
    try:
        actor.bind(audio)
        audio.voiced("Sentence.")
        assert len(counted) == 1
    finally:
        await actor.close()


async def record_of(playback: Playback, tmp_path: Path) -> tuple[list[str], RecordingJournal]:
    journal = RecordingJournal()
    record = MeetingRecord(tmp_path / "record.jsonl")
    log = SpokenLog(journal, record)
    try:
        for event in playback.events:
            log.observe(event)
        return [
            entry.text for entry in record.entries() if isinstance(entry, TwinSpeechEntry)
        ], journal
    finally:
        record.close()


async def test_prevented_clip_leaves_no_twin_speech_in_the_record(tmp_path: Path) -> None:
    playback = Playback()
    playback.gate("invalid")
    try:
        await speak_bound(playback, "The slide shows revenue.")
        twin_speech, journal = await record_of(playback, tmp_path)
        assert twin_speech == []
        [dropped] = journal.events("playback_dropped")
        assert dropped["playback_outcome"] == "prevented"
        assert dropped["local_audio_seconds"] == 0
    finally:
        await playback.engine.close()


async def test_empty_clip_leaves_no_twin_speech_in_the_record(tmp_path: Path) -> None:
    playback = Playback(audio=b"")
    try:
        await playback.engine.speak("Hello there.")
        twin_speech, _ = await record_of(playback, tmp_path)
        assert twin_speech == []
    finally:
        await playback.engine.close()


async def test_written_clip_is_recorded_with_its_attempt_and_outcome(tmp_path: Path) -> None:
    playback = Playback()
    try:
        await playback.engine.speak("Hello there.")
        twin_speech, journal = await record_of(playback, tmp_path)
        assert twin_speech == ["Hello there."]
        [finished] = journal.events("playback_finished")
        assert finished["playback_outcome"] == "completed"
        assert finished["playback_attempt_id"] == playback.attempts[0]
        assert finished["local_audio_seconds"] == pytest.approx(playback.pcm_seconds)
    finally:
        await playback.engine.close()
