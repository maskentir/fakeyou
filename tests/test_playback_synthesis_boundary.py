import asyncio
from collections.abc import AsyncIterator
from typing import Any, override

import pytest

from fakeyou.audio.playback import FRAME_BYTES
from fakeyou.foundation.publication import PublicationContext
from tests._audio_support import FakeSynth, FakeWriter, build_engine


async def test_render_only_speculative_preparation_cannot_start_unbound_synthesis() -> None:
    synth = FakeSynth(audio=b"\x01\x00" * FRAME_BYTES)
    engine = build_engine(synth, FakeWriter(), [])
    engine.configure_publication(
        PublicationContext(
            "meeting", "local_monotonic", render_only=True, runtime_synthesis="local"
        )
    )
    prepared = await engine.prepare(
        "Model text before visual needs were known.", transcript_id="turn"
    )
    assert prepared is not None
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    try:
        assert synth.texts == []
    finally:
        await engine.close()


async def test_render_only_cue_preparation_cannot_synthesize_an_unapproved_sentence() -> None:
    synth = FakeSynth(audio=b"\x01\x00" * FRAME_BYTES)
    engine = build_engine(synth, FakeWriter(), [])
    engine.configure_publication(
        PublicationContext(
            "meeting", "local_monotonic", render_only=True, runtime_synthesis="local"
        )
    )
    await engine.prepare_cue("A model sentence sent to the cue path.")
    try:
        assert synth.texts == []
    finally:
        await engine.close()


async def test_direct_speech_cannot_bypass_a_registered_visual_turn() -> None:
    synth = FakeSynth(audio=b"\x01\x00" * FRAME_BYTES)
    writer = FakeWriter()
    engine = build_engine(synth, writer, [])
    engine.configure_publication(
        PublicationContext(
            "meeting", "local_monotonic", render_only=True, runtime_synthesis="local"
        )
    )
    engine.mark_visual_turn("turn")
    try:
        await engine.speak("Unbound model sentence.", transcript_id="turn")
        await engine.speak_no_visual("Disguised model sentence.", transcript_id="turn")
        assert synth.texts == []
        assert writer.written == []
    finally:
        await engine.close()


async def test_previously_prepared_model_audio_is_discarded_after_visual_classification() -> None:
    synth = FakeSynth(audio=b"\x01\x00" * FRAME_BYTES)
    writer = FakeWriter()
    engine = build_engine(synth, writer, [])
    prepared = await engine.prepare("Previously prepared model sentence.", transcript_id="turn")
    assert prepared is not None
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert synth.texts == [prepared.text]
    engine.configure_publication(
        PublicationContext(
            "meeting", "local_monotonic", render_only=True, runtime_synthesis="local"
        )
    )
    engine.mark_visual_turn("turn")
    try:
        await engine.speak_prepared(prepared)
        assert writer.written == []
        assert synth.texts == [prepared.text]
    finally:
        await engine.close()


async def test_preexisting_cue_audio_cannot_bypass_the_render_only_boundary() -> None:
    synth = FakeSynth(audio=b"\x01\x00" * FRAME_BYTES)
    writer = FakeWriter()
    engine = build_engine(synth, writer, [])
    await engine.prepare_cue("Previously prepared cue.")
    engine.configure_publication(
        PublicationContext(
            "meeting", "local_monotonic", render_only=True, runtime_synthesis="local"
        )
    )
    try:
        assert await engine.play_cue("Previously prepared cue.", transcript_id="turn") is False
        assert writer.written == []
    finally:
        await engine.close()


class RetryingLocalSynth(FakeSynth):
    def __init__(self) -> None:
        super().__init__(audio=b"\x01\x00" * FRAME_BYTES)
        self.attempts = 0

    @override
    async def stream(self, text: str, **kwargs: Any) -> AsyncIterator[bytes]:
        self.attempts += 1
        if self.attempts == 1:
            raise TimeoutError("local synthesis timeout")
        async for chunk in super().stream(text, **kwargs):
            yield chunk


async def test_each_actual_local_retry_has_its_own_attempt_and_terminal_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_publication_activation import Meeting, ask

    meeting = Meeting()
    synth = RetryingLocalSynth()
    monkeypatch.setattr(meeting.engine, "_synthesizer", synth)

    await ask(meeting, "When is beta?", needs=("screen",))

    requested = meeting.journal.events("runtime_synthesis_attempt_requested")
    failed = meeting.journal.events("runtime_synthesis_attempt_failed")
    finished = meeting.journal.events("runtime_synthesis_attempt_finished")
    assert synth.attempts == 2
    assert len(requested) == 2
    assert all(row["publication_id"] == meeting.bindings[0].publication_id for row in requested)
    assert len({row["attempt_id"] for row in requested}) == 2
    assert failed[0]["attempt_id"] == requested[0]["attempt_id"]
    assert finished[0]["attempt_id"] == requested[1]["attempt_id"]
    assert all(row["physical_provider_request"] is False for row in requested)


async def test_local_mode_cannot_authorize_an_unproven_synthesizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_publication_activation import Meeting, ask

    meeting = Meeting()
    monkeypatch.setattr(meeting.synth, "local_only", False)

    turn = await ask(meeting, "When is beta?", needs=("screen",))

    assert meeting.synth.texts == []
    assert turn.counted == []
    assert meeting.journal.events("runtime_synthesis_attempt_requested") == []


class RevokingLocalSynth(RetryingLocalSynth):
    @override
    async def stream(self, text: str, **kwargs: Any) -> AsyncIterator[bytes]:
        self.local_only = False
        async for chunk in super().stream(text, **kwargs):
            yield chunk


async def test_retry_rechecks_local_authorization_before_opening_another_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_publication_activation import Meeting, ask

    meeting = Meeting()
    synth = RevokingLocalSynth()
    monkeypatch.setattr(meeting.engine, "_synthesizer", synth)

    turn = await ask(meeting, "When is beta?", needs=("screen",))

    assert synth.attempts == 1
    assert len(meeting.journal.events("runtime_synthesis_attempt_requested")) == 1
    assert len(meeting.journal.events("runtime_synthesis_attempt_failed")) == 1
    assert turn.counted == []
