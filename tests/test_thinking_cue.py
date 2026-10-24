import asyncio
from collections.abc import Sequence
from typing import override

import pytest

from fakeyou.conversation.meeting_turns import (
    MeetingTurns,
    TurnInteraction,
    TurnTiming,
    build_meeting_turns,
)
from fakeyou.conversation.thinking_cue import THINKING_CUE_LINES, ThinkingCue, thinking_cue_for
from fakeyou.foundation.acks import BARE_ACK_VOCABULARY
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import FakeResponder
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal

TWIN = "Rani Twin"
QUESTION = f"{TWIN}, when is the demo?"


class CueAudio(FakeReplyAudio):
    def __init__(self) -> None:
        super().__init__()
        self.prepared: list[tuple[str, ...]] = []
        self.timeline: list[tuple[str, str]] = []

    def prepare_cues(self, texts: Sequence[str]) -> None:
        self.prepared.append(tuple(texts))

    async def play_cue(self, text: str) -> bool:
        self.timeline.append(("cue", text))
        return True

    @override
    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None:
        del language
        self.timeline.append(("reply", text))
        await super().speak(text, sentence_index=sentence_index, transcript_id=transcript_id)


def cue_turns(
    responder: FakeResponder,
    *,
    after_seconds: float | None,
    journal: RecordingJournal,
    others_speaking: bool = False,
) -> tuple[MeetingTurns, CueAudio]:
    audio = CueAudio()
    turns = build_meeting_turns(
        responder,
        interaction=TurnInteraction(twin_names=(TWIN,), others_speaking=lambda: others_speaking),
        timing=TurnTiming(thinking_cue_after_seconds=after_seconds),
        diagnostics=Diagnostics(journal=journal),
    )
    turns.lifecycle.bind(audio)
    return turns, audio


async def answer_slowly(turns: MeetingTurns, responder: FakeResponder, gate: asyncio.Event) -> None:
    final = asyncio.create_task(turns.ingress.handle_final(event("t1", QUESTION)))
    await responder.started.wait()
    await asyncio.sleep(0.05)
    gate.set()
    await final


async def test_a_slow_reply_is_preceded_by_one_thinking_cue() -> None:
    journal = RecordingJournal()
    gate = asyncio.Event()
    responder = FakeResponder(gate)
    turns, audio = cue_turns(responder, after_seconds=0.0, journal=journal)

    await answer_slowly(turns, responder, gate)
    await turns.close()

    assert audio.timeline == [
        ("cue", THINKING_CUE_LINES["id"][0]),
        ("reply", QUESTION),
    ]
    assert [row["transcript_id"] for row in journal.events("thinking_cue_requested")] == ["t1"]
    assert audio.spoken == [QUESTION]


async def test_the_twin_prepares_its_cues_when_it_binds_to_the_audio() -> None:
    _, audio = cue_turns(FakeResponder(), after_seconds=0.5, journal=RecordingJournal())

    assert audio.prepared == [THINKING_CUE_LINES["id"]]


async def test_a_quick_reply_needs_no_cue() -> None:
    journal = RecordingJournal()
    turns, audio = cue_turns(FakeResponder(), after_seconds=0.5, journal=journal)

    await turns.ingress.handle_final(event("t1", QUESTION))
    await turns.close()

    assert audio.timeline == [("reply", QUESTION)]
    assert journal.events("thinking_cue_requested") == []


async def test_no_cue_while_someone_else_holds_the_floor() -> None:
    journal = RecordingJournal()
    gate = asyncio.Event()
    responder = FakeResponder(gate)
    turns, audio = cue_turns(responder, after_seconds=0.0, journal=journal, others_speaking=True)

    await answer_slowly(turns, responder, gate)
    await turns.close()

    assert [kind for kind, _ in audio.timeline] == ["reply"]
    assert [row["reason"] for row in journal.events("thinking_cue_skipped")] == ["floor_busy"]


async def test_cues_stay_off_without_a_delay() -> None:
    gate = asyncio.Event()
    responder = FakeResponder(gate)
    turns, audio = cue_turns(responder, after_seconds=None, journal=RecordingJournal())

    await answer_slowly(turns, responder, gate)
    await turns.close()

    assert audio.prepared == []
    assert [kind for kind, _ in audio.timeline] == ["reply"]


def test_cue_lines_rotate_so_the_twin_does_not_repeat_itself() -> None:
    cue = ThinkingCue(("Hmm…", "Hmm, one sec…"), 0.5)

    assert [cue.next_line() for _ in range(3)] == ["Hmm…", "Hmm, one sec…", "Hmm…"]


def test_cue_lines_exist_for_every_meeting_language_without_agreeing_to_anything() -> None:
    for language in ("id", "en"):
        cue = thinking_cue_for(language, 0.5)
        assert cue is not None
        words = {word.strip(",.…").casefold() for line in cue.lines for word in line.split()}
        assert words & BARE_ACK_VOCABULARY == set()


@pytest.mark.parametrize(("lines", "after_seconds"), [((), 0.5), (("Hmm…",), -1.0)])
def test_a_cue_needs_lines_and_a_non_negative_delay(
    lines: tuple[str, ...], after_seconds: float
) -> None:
    with pytest.raises(ValueError, match="thinking cue"):
        ThinkingCue(lines, after_seconds)
