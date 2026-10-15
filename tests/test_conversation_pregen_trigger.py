import asyncio

from fakeyou.conversation.conversation import (
    ConversationLoop,
    LoopGeneration,
)
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal
from tests._pregen_support import StreamingResponder, journal_has


async def test_pregeneration_adopts_exact_match_without_second_call(
    journal: RecordingJournal,
) -> None:
    responder = StreamingResponder(["Yes I can.", "Let me check."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_transcript(event("f1", "can you help me today?", finalized=True))
    await loop.wait_idle()
    assert audio.spoken == ["Yes I can.", "Let me check."]
    assert len(responder.calls) == 1
    assert journal_has(journal, "pregeneration_finished", outcome="adopted")
    await loop.close()


async def test_pregeneration_discards_on_mismatch_and_uses_normal_path(
    journal: RecordingJournal,
) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    await loop.handle_transcript(event("f1", "cancel everything now", finalized=True))
    await loop.wait_idle()
    assert len(responder.calls) == 2
    assert journal_has(journal, "pregeneration_finished", outcome="discarded")
    await loop.close()


async def test_pregeneration_stays_off_by_default(journal: RecordingJournal) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    assert responder.calls == []
    await loop.handle_transcript(event("f1", "can you help me today?", finalized=True))
    await loop.wait_idle()
    assert len(responder.calls) == 1
    await loop.close()


async def test_note_vad_fires_pregen_and_adopts_exact_final(journal: RecordingJournal) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today")
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert journal_has(journal, "pregeneration_started")
    await loop.handle_transcript(event("f1", "can you help me today", finalized=True))
    await loop.wait_idle()
    assert audio.spoken == ["Yes I can."]
    assert len(responder.calls) == 1
    assert journal_has(journal, "pregeneration_finished", outcome="adopted")
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.close()


async def test_note_vad_stays_silent_on_speech_start(journal: RecordingJournal) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today")
    await loop.note_vad(active=True)
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_note_vad_stays_silent_without_interim(journal: RecordingJournal) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_note_vad_fires_on_short_interim(journal: RecordingJournal) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("see you")
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert len(journal.events("pregeneration_started")) == 1
    await loop.close()


async def test_note_vad_stays_silent_on_empty_interim(journal: RecordingJournal) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("  ")
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_note_vad_stays_silent_when_disabled(journal: RecordingJournal) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today")
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_note_vad_stays_silent_when_busy(journal: RecordingJournal) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today")
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_post_stop_interim_starts_pregen_with_single_word(journal: RecordingJournal) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert responder.calls == []
    await loop.handle_interim("hello")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert journal_has(journal, "pregeneration_started")
    await loop.close()


async def test_post_stop_interim_restarts_pregen_on_changed_words(
    journal: RecordingJournal,
) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me")
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_interim("can you help me today")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 2
    assert len(journal.events("pregeneration_started")) == 2
    assert journal_has(journal, "pregeneration_finished", outcome="discarded", reason="superseded")
    await loop.close()


async def test_post_stop_interim_keeps_pregen_on_identical_words(journal: RecordingJournal) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me")
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_interim("can you help me")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert len(journal.events("pregeneration_started")) == 1
    assert journal.events("pregeneration_finished") == []
    await loop.close()


async def test_mid_speech_interim_starts_no_post_stop_pregen(journal: RecordingJournal) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.note_vad(active=True)
    await loop.handle_interim("hello")
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_vad_start_clears_post_stop_pregen_flag(journal: RecordingJournal) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.note_vad(active=True)
    await loop.note_vad(active=False)
    await loop.note_vad(active=True)
    await loop.handle_interim("hello")
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_final_clears_post_stop_pregen_flag(journal: RecordingJournal) -> None:
    responder = StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.note_vad(active=False)
    await loop.handle_transcript(event("f1", "can you help me today", finalized=True))
    await loop.wait_idle()
    assert len(responder.calls) == 1
    await loop.handle_interim("hello")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert journal.events("pregeneration_started") == []
    await loop.close()
