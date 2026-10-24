from collections.abc import Callable, Sequence
from typing import override

import pytest

from fakeyou.conversation.meeting_turns import build_meeting_turns
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.session import ConversationError
from fakeyou.conversation.turn_recovery import MAX_CONSECUTIVE_TURN_FAILURES, TURN_FAILED_LINES
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import FakeResponder
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal

TURN_FAILED_LINE = TURN_FAILED_LINES["id"]


class ScriptedResponder(FakeResponder):
    def __init__(self, outcomes: list[str]) -> None:
        super().__init__()
        self._outcomes = list(outcomes)

    @override
    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> SpokenChunk:
        if self._outcomes.pop(0) == "fail":
            self.calls.append(list(events))
            raise RuntimeError("model unavailable")
        return await super().respond(events, on_tool_start)


async def test_one_failed_turn_says_a_line_and_keeps_listening() -> None:
    audio = FakeReplyAudio()
    loop = build_meeting_turns(ScriptedResponder(["fail", "ok"]))
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "first question"))
    await loop.lifecycle.wait_idle()
    await loop.ingress.receive_final(event("t2", "second question"))
    await loop.lifecycle.wait_idle()

    loop.lifecycle.raise_if_failed()
    assert loop.lifecycle.phase != "failed"
    assert audio.spoken == [TURN_FAILED_LINE, "second question"]
    await loop.lifecycle.close()


async def test_consecutive_failures_fail_the_loop_at_the_limit() -> None:
    audio = FakeReplyAudio()
    loop = build_meeting_turns(ScriptedResponder(["fail"] * MAX_CONSECUTIVE_TURN_FAILURES))
    loop.lifecycle.bind(audio)
    for index in range(MAX_CONSECUTIVE_TURN_FAILURES - 1):
        await loop.ingress.receive_final(event(f"t{index}", f"question {index}"))
        await loop.lifecycle.wait_idle()
    await loop.ingress.receive_final(event("last", "final question"))

    with pytest.raises(ConversationError, match="conversation failed"):
        await loop.lifecycle.wait_idle()
    assert audio.spoken == [TURN_FAILED_LINE] * (MAX_CONSECUTIVE_TURN_FAILURES - 1)
    await loop.lifecycle.close()


async def test_a_successful_turn_resets_the_failure_streak() -> None:
    limit = MAX_CONSECUTIVE_TURN_FAILURES
    outcomes = ["fail"] * (limit - 1) + ["ok"] + ["fail"] * (limit - 1)
    audio = FakeReplyAudio()
    loop = build_meeting_turns(ScriptedResponder(outcomes))
    loop.lifecycle.bind(audio)
    for index in range(len(outcomes)):
        await loop.ingress.receive_final(event(f"t{index}", f"question {index}"))
        await loop.lifecycle.wait_idle()

    loop.lifecycle.raise_if_failed()
    assert loop.lifecycle.phase != "failed"
    assert audio.spoken.count(TURN_FAILED_LINE) == 2 * (limit - 1)
    await loop.lifecycle.close()


async def test_failed_turn_is_journaled_with_its_streak(journal: RecordingJournal) -> None:
    loop = build_meeting_turns(
        ScriptedResponder(["fail", "fail"]), diagnostics=Diagnostics(journal=journal)
    )
    loop.lifecycle.bind(FakeReplyAudio())
    await loop.ingress.receive_final(event("t1", "one"))
    await loop.lifecycle.wait_idle()
    await loop.ingress.receive_final(event("t2", "two"))
    await loop.lifecycle.wait_idle()

    failures = [
        (row["transcript_id"], row["error"], row["consecutive"])
        for row in journal.events("turn_failed")
    ]
    assert failures == [("t1", "RuntimeError", 1), ("t2", "RuntimeError", 2)]
    await loop.lifecycle.close()
