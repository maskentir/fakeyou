import hashlib

from fakeyou.audio.audio import StreamCallbacks, StreamComponents, open_meeting_audio
from fakeyou.audio.playback import PlaybackEvent
from fakeyou.foundation.publication import PublicationContext, PublicationDecision, SentenceBinding
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.speech.contract import speak_no_visual
from tests._audio_fakes import FakeSTT
from tests._audio_support import FakeCapture, FakeSynth, FakeWriter, build_engine


async def test_session_construction_preserves_explicit_publication_context() -> None:
    events: list[PlaybackEvent] = []
    writer = FakeWriter()
    engine = build_engine(FakeSynth(), writer, events)
    context = PublicationContext("meeting", "test", now=lambda: 100.0)

    async def on_transcript(_event: TranscriptUpdate) -> None:
        return None

    async with open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=engine, publication=context),
    ) as session:
        assert session.publication_context is context
        prepared = await session.prepare("Hello.", sentence_index=0, transcript_id="turn")
        assert prepared is not None
        binding = SentenceBinding(
            "publication",
            "meeting",
            "turn",
            "turn",
            "request",
            0,
            hashlib.sha256(b"Hello.").hexdigest(),
            None,
            (),
            (),
            "no_visual",
        )
        session.bind_publication(prepared, binding)
        await session.speak_prepared(prepared)
        assert writer.written
        [started] = [e for e in events if e.kind == "playback_audio_started"]
        assert started.utterance.publication is binding


async def test_initial_greeting_plays_and_unbound_visual_claim_is_refused() -> None:
    events: list[PlaybackEvent] = []
    writer = FakeWriter()
    engine = build_engine(FakeSynth(), writer, events)
    gated: list[SentenceBinding] = []

    async def gate(binding: SentenceBinding) -> PublicationDecision:
        gated.append(binding)
        return PublicationDecision("valid", "ok", 100.0, "test")

    context = PublicationContext("meeting", "test", gate, now=lambda: 100.0)

    async def on_transcript(_event: TranscriptUpdate) -> None:
        return None

    async with open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=engine, publication=context),
    ) as session:
        await speak_no_visual(session, "Hi everyone, I just joined.")
        greeted = len(writer.written)
        assert greeted
        await session.speak("The slide now shows the Q3 revenue chart.", transcript_id="turn")
        assert len(writer.written) == greeted
        assert gated == []
        assert [e.kind for e in events][-1] == "playback_dropped"
