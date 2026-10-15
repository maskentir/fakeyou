import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from typing import override

import pytest

from fakeyou.conversation.conversation import (
    ConversationLoop,
    LoopGeneration,
)
from fakeyou.conversation.session import ConversationError
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.provider_faults import ProviderFault
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal


class _AdoptingResponder:
    def __init__(self, merge_error: BaseException | None = None) -> None:
        self.merge_error = merge_error
        self.merged: list[tuple[str, ...]] = []

    def stream_sentences(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
    ) -> AsyncIterator[str]:
        del request_id
        del on_tool_start
        del events
        return self._generate()

    async def _generate(self) -> AsyncIterator[str]:
        yield "Yes I can."

    async def complete_adopted_pregen(
        self, events: Sequence[TranscriptEvent], sentences: Sequence[str]
    ) -> None:
        del events
        if self.merge_error is not None:
            raise self.merge_error
        self.merged.append(tuple(sentences))


class _FailingAudio(FakeReplyAudio):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    @override
    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> None:
        raise self.error


async def _adopted_turn(
    responder: _AdoptingResponder, audio: FakeReplyAudio, journal: RecordingJournal
) -> tuple[ConversationLoop, FaultReporter]:
    reporter = FaultReporter(journal=journal)
    loop = ConversationLoop(
        responder,
        generation=LoopGeneration(sentence_streaming=True, pre_generate_enabled=True),
        diagnostics=Diagnostics(faults=reporter, journal=journal),
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    await loop.handle_transcript(event("f1", "can you help me today?"))
    [adopted] = journal.events("pregeneration_finished")
    assert adopted["outcome"] == "adopted"
    return loop, reporter


async def test_adopted_turn_merges_spoken_sentences(journal: RecordingJournal) -> None:
    responder, audio = _AdoptingResponder(), FakeReplyAudio()
    loop, _reporter = await _adopted_turn(responder, audio, journal)

    await loop.wait_idle()

    assert audio.spoken == ["Yes I can."]
    assert responder.merged == [("Yes I can.",)]
    await loop.close()


async def test_adopted_merge_checkpoint_error_is_recorded_as_fault(
    journal: RecordingJournal,
) -> None:
    responder, audio = _AdoptingResponder(TimeoutError("store slow")), FakeReplyAudio()
    loop, reporter = await _adopted_turn(responder, audio, journal)

    await loop.wait_idle()

    (summary,) = reporter.summaries()
    assert summary.operation == "adopt_merge"
    assert audio.spoken == ["Yes I can."]
    await loop.close()


async def test_adopted_merge_unexpected_error_is_one_failed_turn(
    journal: RecordingJournal,
) -> None:
    responder, audio = _AdoptingResponder(ValueError("bug")), FakeReplyAudio()
    loop, _reporter = await _adopted_turn(responder, audio, journal)

    await loop.wait_idle()

    loop.raise_if_failed()
    assert [row["error"] for row in journal.events("turn_failed")] == ["ValueError"]
    await loop.close()


@pytest.mark.parametrize("error", [ProviderFault("agent", "unknown"), ValueError("bug")])
async def test_adopted_turn_failure_fails_the_conversation(
    journal: RecordingJournal, error: Exception
) -> None:
    loop, reporter = await _adopted_turn(_AdoptingResponder(), _FailingAudio(error), journal)

    with pytest.raises(ConversationError, match="conversation failed") as caught:
        await loop.wait_idle()

    assert caught.value.__cause__ is error
    assert reporter.total() >= 1
    await loop.close()
