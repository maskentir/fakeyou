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
from tests._pregen_support import DIFF_KEYS, StreamingResponder, journal_has


class _GatedStreamingResponder:
    def __init__(self) -> None:
        self.calls: list[list[TranscriptEvent]] = []
        self.release = asyncio.Event()

    def stream_sentences(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
    ) -> AsyncIterator[str]:
        del on_tool_start
        del request_id
        self.calls.append(list(events))
        return self._generate()

    async def _generate(self) -> AsyncIterator[str]:
        await self.release.wait()
        yield "Yes I can."


async def test_pregen_fuzzy_restart_suppresses_suffix_extension(journal: RecordingJournal) -> None:
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
    [started] = journal.events("pregeneration_started")
    await loop.handle_interim("can you help me today")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert len(journal.events("pregeneration_started")) == 1
    [row] = journal.events("pregeneration_restart_skipped")
    assert row["request_id"] == started["request_id"]
    assert row["diff_class"] == "prefix_extend"
    for key in DIFF_KEYS:
        assert key in row
    assert journal.events("pregeneration_finished") == []
    await loop.handle_transcript(event("f1", "can you help me today", finalized=True))
    responder.release.set()
    await loop.wait_idle()
    [adopted] = journal.events("pregeneration_finished")
    assert adopted["outcome"] == "adopted"
    assert adopted["diff_class"] == "exact"
    assert len(responder.calls) == 1
    assert audio.spoken == ["Yes I can."]
    await loop.close()


async def test_pregen_fuzzy_restart_restarts_on_single_substitution(
    journal: RecordingJournal,
) -> None:
    responder = StreamingResponder(["Yes I can."])
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
    await loop.handle_interim("can you help him")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 2
    first, second = journal.events("pregeneration_started")
    assert second["request_id"] != first["request_id"]
    assert journal_has(journal, "pregeneration_finished", outcome="discarded", reason="superseded")
    assert journal.events("pregeneration_restart_skipped") == []
    await loop.close()


async def test_pregen_fuzzy_restart_restarts_on_small_non_suffix_drift(
    journal: RecordingJournal,
) -> None:
    responder = StreamingResponder(["Yes I can."])
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


async def test_pregen_fuzzy_restart_suppresses_small_drift_on_long_utterance(
    journal: RecordingJournal,
) -> None:
    responder = StreamingResponder(["Yes I can."])
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
    [started] = journal.events("pregeneration_started")
    await loop.handle_interim("please can you help me tonight")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert len(journal.events("pregeneration_started")) == 1
    [row] = journal.events("pregeneration_restart_skipped")
    assert row["request_id"] == started["request_id"]
    assert row["dist"] == 1
    for key in DIFF_KEYS:
        assert key in row
    assert journal.events("pregeneration_finished") == []
    await loop.handle_transcript(event("f1", "please can you help me tonight", finalized=True))
    await loop.wait_idle()
    [adopted] = journal.events("pregeneration_finished")
    assert adopted["outcome"] == "adopted"
    assert adopted["diff_class"] == "exact"
    assert len(responder.calls) == 1
    assert audio.spoken == ["Yes I can."]
    await loop.close()


async def test_pregen_restart_without_fuzzy_flag_restarts_on_suffix_extension(
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
    assert journal_has(journal, "pregeneration_finished", outcome="discarded", reason="superseded")
    assert journal.events("pregeneration_restart_skipped") == []
    await loop.close()


async def test_pregen_fuzzy_restart_suppressed_pregen_rejects_inexact_final(
    journal: RecordingJournal,
) -> None:
    responder = StreamingResponder(["Yes I can."])
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
    for key in DIFF_KEYS:
        assert row[key] == expected[key]
    assert not journal_has(journal, "pregeneration_finished", outcome="adopted")
    await loop.close()


def test_pregen_fuzzy_restart_default_follows_settings_flag() -> None:
    import inspect

    from fakeyou.config.settings import DEFAULTS

    assert DEFAULTS.conversation.pregen_fuzzy_restart_enabled is False
    params = inspect.signature(LoopGeneration).parameters
    assert (
        params["pregen_fuzzy_restart_enabled"].default
        == DEFAULTS.conversation.pregen_fuzzy_restart_enabled
    )
