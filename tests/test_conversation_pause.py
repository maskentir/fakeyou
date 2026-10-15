import asyncio

from fakeyou.conversation.conversation import ConversationLoop
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import FakeResponder
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal


async def test_paused_loop_drops_transcripts_and_stays_silent(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
    loop.bind(audio)

    await loop.pause()
    await loop.handle_transcript(event("t1", "are you there?"))
    await loop.wait_idle()

    assert responder.calls == []
    assert audio.spoken == []
    assert [row["reason"] for row in journal.events("transcript_suppressed")] == ["paused"]
    await loop.close()


async def test_resumed_loop_answers_again() -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder)
    loop.bind(audio)

    await loop.pause()
    loop.resume()
    await loop.handle_transcript(event("t1", "are you there now?"))
    await loop.wait_idle()

    assert audio.spoken == ["are you there now?"]
    await loop.close()


async def test_pause_during_a_turn_drops_the_reply(journal: RecordingJournal) -> None:
    gate = asyncio.Event()
    responder, audio = FakeResponder(gate), FakeReplyAudio()
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
    loop.bind(audio)

    await loop.handle_transcript(event("t1", "long question"))
    await responder.started.wait()
    await loop.pause()
    gate.set()
    await loop.wait_idle()

    assert audio.spoken == []
    assert [row["reason"] for row in journal.events("turn_suppressed")] == ["paused"]
    await loop.close()
