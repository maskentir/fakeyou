import asyncio
from collections.abc import AsyncIterator, Callable, Sequence

from fakeyou.conversation.conversation import (
    ConversationLoop,
    LoopGeneration,
)
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.foundation.utterance_quality import describe_word_diff, normalize_words
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal
from tests._pregen_support import DIFF_KEYS, StreamingResponder


async def test_pregen_superseded_journal_carries_word_diff(journal: RecordingJournal) -> None:
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
    [row] = [
        row for row in journal.events("pregeneration_finished") if row.get("reason") == "superseded"
    ]
    expected = describe_word_diff(
        normalize_words("can you help me"), normalize_words("can you help me today")
    )
    for key in DIFF_KEYS:
        assert row[key] == expected[key]
    await loop.close()


async def test_pregen_mismatched_journal_carries_word_diff(journal: RecordingJournal) -> None:
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
    assert len(responder.calls) == 1
    await loop.handle_transcript(event("f1", "cancel everything now", finalized=True))
    await loop.wait_idle()
    [row] = [
        row for row in journal.events("pregeneration_finished") if row.get("reason") == "mismatched"
    ]
    expected = describe_word_diff(
        normalize_words("can you help me today?"), normalize_words("cancel everything now")
    )
    for key in DIFF_KEYS:
        assert row[key] == expected[key]
    await loop.close()


async def test_pregen_adopted_journal_carries_exact_diff(journal: RecordingJournal) -> None:
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
    [row] = [
        row for row in journal.events("pregeneration_finished") if row.get("outcome") == "adopted"
    ]
    assert row["dist"] == 0
    assert row["diff_class"] == "exact"
    assert row["words_pre"] == row["words_final"] == len(normalize_words("can you help me today?"))
    await loop.close()


class _FailingStreamResponder:
    def stream_sentences(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
    ) -> AsyncIterator[str]:
        del events
        del on_tool_start
        del request_id
        raise RuntimeError("stream failed")


async def test_pregen_error_discard_journal_carries_no_diff(journal: RecordingJournal) -> None:
    responder = _FailingStreamResponder()
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
    assert len(journal.events("pregeneration_started")) == 1
    await loop.handle_transcript(event("f1", "can you help me today?", finalized=True))
    await loop.wait_idle()
    [row] = [
        row for row in journal.events("pregeneration_finished") if row.get("reason") == "error"
    ]
    for key in DIFF_KEYS:
        assert key not in row
    await loop.close()


async def test_pregen_journal_diff_values_are_metadata_only(journal: RecordingJournal) -> None:
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
    await loop.handle_interim("can you help me today")
    await asyncio.sleep(0.05)
    await loop.handle_transcript(event("f1", "can you help me today", finalized=True))
    await loop.wait_idle()
    rows = journal.events("pregeneration_finished")
    assert len(rows) >= 2
    allowed = {"exact", "prefix_extend", "prefix_truncate", "single_subst", "other"}
    for row in rows:
        for key in ("dist", "words_pre", "words_final"):
            if key in row:
                assert isinstance(row[key], int)
                assert not isinstance(row[key], bool)
        if "norm_dist_bucket" in row:
            assert isinstance(row["norm_dist_bucket"], float)
        if "diff_class" in row:
            assert row["diff_class"] in allowed
        for key in DIFF_KEYS:
            if key in row:
                assert "can you help" not in str(row[key])
    await loop.close()
