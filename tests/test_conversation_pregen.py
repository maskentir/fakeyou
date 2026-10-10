import asyncio

import pytest

from fakeyou.conversation.conversation import (
    ConversationLoop,
    LoopGeneration,
)
from fakeyou.conversation.session import ConversationError
from fakeyou.foundation.transcript import (
    TranscriptEvent,
)
from fakeyou.foundation.utterance_quality import describe_word_diff, normalize_words
from fakeyou.observability.diagnostics import Diagnostics
from tests._conversation_support import event
from tests.fakes import FakeReplyAudio


class _StreamingResponder:
    def __init__(self, sentences: list[str]) -> None:
        self.sentences = sentences
        self.calls: list[list[TranscriptEvent]] = []
        self.released = asyncio.Event()

    def stream_sentences(self, events, on_tool_start, *, request_id: str):
        self.calls.append(list(events))
        return self._generate()

    async def _generate(self):
        for sentence in self.sentences:
            yield sentence


def journal_has(journal, name, **fields):
    return any(
        all(row.get(key) == value for key, value in fields.items()) for row in journal.events(name)
    )


async def test_pregeneration_adopts_exact_match_without_second_call(journal) -> None:
    responder = _StreamingResponder(["Yes I can.", "Let me check."])
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


async def test_pregeneration_discards_on_mismatch_and_uses_normal_path(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_pregeneration_stays_off_by_default(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_note_vad_fires_pregen_and_adopts_exact_final(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_note_vad_stays_silent_on_speech_start(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_note_vad_stays_silent_without_interim(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_note_vad_fires_on_short_interim(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_note_vad_stays_silent_on_empty_interim(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_note_vad_stays_silent_when_disabled(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_note_vad_stays_silent_when_busy(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_post_stop_interim_starts_pregen_with_single_word(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_post_stop_interim_restarts_pregen_on_changed_words(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_post_stop_interim_keeps_pregen_on_identical_words(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_mid_speech_interim_starts_no_post_stop_pregen(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_vad_start_clears_post_stop_pregen_flag(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


async def test_final_clears_post_stop_pregen_flag(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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


_DIFF_KEYS = ("dist", "words_pre", "words_final", "norm_dist_bucket", "diff_class")


async def test_pregen_superseded_journal_carries_word_diff(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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
    for key in _DIFF_KEYS:
        assert row[key] == expected[key]
    await loop.close()


async def test_pregen_mismatched_journal_carries_word_diff(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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
    for key in _DIFF_KEYS:
        assert row[key] == expected[key]
    await loop.close()


async def test_pregen_adopted_journal_carries_exact_diff(journal) -> None:
    responder = _StreamingResponder(["Yes I can.", "Let me check."])
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
    def stream_sentences(self, events, on_tool_start, *, request_id: str):
        raise RuntimeError("stream failed")


async def test_pregen_error_discard_journal_carries_no_diff(journal) -> None:
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
    with pytest.raises(ConversationError):
        await loop.wait_idle()
    [row] = [
        row for row in journal.events("pregeneration_finished") if row.get("reason") == "error"
    ]
    for key in _DIFF_KEYS:
        assert key not in row
    await loop.close()


async def test_pregen_journal_diff_values_are_metadata_only(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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
                assert isinstance(row[key], int) and not isinstance(row[key], bool)
        if "norm_dist_bucket" in row:
            assert isinstance(row["norm_dist_bucket"], float)
        if "diff_class" in row:
            assert row["diff_class"] in allowed
        for key in _DIFF_KEYS:
            if key in row:
                assert "can you help" not in str(row[key])
    await loop.close()


class _GatedStreamingResponder:
    def __init__(self) -> None:
        self.calls: list[list[TranscriptEvent]] = []
        self.release = asyncio.Event()

    def stream_sentences(self, events, on_tool_start, *, request_id: str):
        self.calls.append(list(events))
        return self._generate()

    async def _generate(self):
        await self.release.wait()
        yield "Yes I can."


async def test_pregen_fuzzy_restart_suppresses_suffix_extension(journal) -> None:
    responder = _GatedStreamingResponder()
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(
            sentence_streaming=True, pre_generate_enabled=True, pregen_fuzzy_restart_enabled=True
        ),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me")
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    before = loop._pregen
    assert before is not None
    request_id = before.request_id
    task = before.task
    assert task is not None and not task.done()
    await loop.handle_interim("can you help me today")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert len(journal.events("pregeneration_started")) == 1
    current = loop._pregen
    assert current is not None
    assert current.request_id == request_id
    assert current.task is task
    assert not task.done()
    assert current.words == normalize_words("can you help me today")
    [row] = journal.events("pregeneration_restart_skipped")
    assert row["request_id"] == request_id
    assert row["diff_class"] == "prefix_extend"
    for key in _DIFF_KEYS:
        assert key in row
    assert journal.events("pregeneration_finished") == []
    responder.release.set()
    await loop.close()


async def test_pregen_fuzzy_restart_restarts_on_single_substitution(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(
            sentence_streaming=True, pre_generate_enabled=True, pregen_fuzzy_restart_enabled=True
        ),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me")
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert loop._pregen is not None
    before_id = loop._pregen.request_id
    await loop.handle_interim("can you help him")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 2
    assert loop._pregen is not None
    assert loop._pregen.request_id != before_id
    assert journal_has(journal, "pregeneration_finished", outcome="discarded", reason="superseded")
    assert journal.events("pregeneration_restart_skipped") == []
    await loop.close()


async def test_pregen_fuzzy_restart_restarts_on_small_non_suffix_drift(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(
            sentence_streaming=True, pre_generate_enabled=True, pregen_fuzzy_restart_enabled=True
        ),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("see you")
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_interim("see them")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 2
    assert journal_has(journal, "pregeneration_finished", outcome="discarded", reason="superseded")
    assert journal.events("pregeneration_restart_skipped") == []
    await loop.close()


async def test_pregen_fuzzy_restart_suppresses_small_drift_on_long_utterance(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(
            sentence_streaming=True, pre_generate_enabled=True, pregen_fuzzy_restart_enabled=True
        ),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("please can you help me today")
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert loop._pregen is not None
    request_id = loop._pregen.request_id
    await loop.handle_interim("please can you help me tonight")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert len(journal.events("pregeneration_started")) == 1
    current = loop._pregen
    assert current is not None
    assert current.request_id == request_id
    assert current.words == normalize_words("please can you help me tonight")
    [row] = journal.events("pregeneration_restart_skipped")
    assert row["request_id"] == request_id
    assert row["dist"] == 1
    for key in _DIFF_KEYS:
        assert key in row
    assert journal.events("pregeneration_finished") == []
    await loop.close()


async def test_pregen_restart_without_fuzzy_flag_restarts_on_suffix_extension(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
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
    assert journal_has(journal, "pregeneration_finished", outcome="discarded", reason="superseded")
    assert journal.events("pregeneration_restart_skipped") == []
    await loop.close()


async def test_pregen_fuzzy_restart_suppressed_pregen_rejects_inexact_final(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(
            sentence_streaming=True, pre_generate_enabled=True, pregen_fuzzy_restart_enabled=True
        ),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me")
    await loop.note_vad(active=False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_interim("can you help me today")
    await asyncio.sleep(0.05)
    assert len(journal.events("pregeneration_restart_skipped")) == 1
    await loop.handle_transcript(event("f1", "something else entirely", finalized=True))
    await loop.wait_idle()
    assert len(responder.calls) == 2
    [row] = [
        row for row in journal.events("pregeneration_finished") if row.get("reason") == "mismatched"
    ]
    expected = describe_word_diff(
        normalize_words("can you help me today"), normalize_words("something else entirely")
    )
    for key in _DIFF_KEYS:
        assert row[key] == expected[key]
    assert not journal_has(journal, "pregeneration_finished", outcome="adopted")
    await loop.close()


def test_pregen_fuzzy_restart_default_follows_settings_flag():
    import inspect

    from fakeyou.config.settings import DEFAULTS

    assert DEFAULTS.conversation.pregen_fuzzy_restart_enabled is False
    params = inspect.signature(LoopGeneration).parameters
    assert (
        params["pregen_fuzzy_restart_enabled"].default
        == DEFAULTS.conversation.pregen_fuzzy_restart_enabled
    )
