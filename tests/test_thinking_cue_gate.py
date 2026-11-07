import asyncio
from dataclasses import dataclass, field
from functools import partial
from types import SimpleNamespace

from fakeyou.conversation.thinking_cue import CueState, CueTurn, ThinkingCue
from fakeyou.conversation.turn_graph import _cue_blocker, _speak_gated_opener
from tests._persistence_fakes import RecordingJournal
from tests._turn_context import TurnTestContext


@dataclass
class Gate:
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    resumed: bool = False


@dataclass
class CueSpeaker:
    cues: list[str] = field(default_factory=list)
    is_speaking: bool = False

    async def play_cue(self, text: str) -> bool:
        self.cues.append(text)
        return True


def opener_turn(runtime: TurnTestContext, gate: Gate, state: CueState) -> CueTurn:
    sink = SimpleNamespace(started=False)
    blocked = partial(_cue_blocker, runtime, sink, (0, 4), state, gate)
    return CueTurn("t1", blocked, state=state)


async def test_the_opener_waits_for_the_early_turn_to_settle() -> None:
    gate = Gate()
    speaker = CueSpeaker()
    state = CueState()
    runtime = TurnTestContext(speaker=speaker, journal=RecordingJournal())
    cue = ThinkingCue(lines=("Hmm.",), after_seconds=30.0)
    task = asyncio.create_task(
        _speak_gated_opener(cue, speaker, runtime.journal, opener_turn(runtime, gate, state), gate)
    )
    await asyncio.sleep(0.02)
    assert speaker.cues == []

    gate.settled.set()
    await asyncio.wait_for(task, 1)

    assert speaker.cues == ["Hmm."]
    assert state.played


async def test_a_withdrawn_early_turn_never_speaks_its_opener() -> None:
    gate = Gate(resumed=True)
    gate.settled.set()
    speaker = CueSpeaker()
    journal = RecordingJournal()
    state = CueState()
    runtime = TurnTestContext(speaker=speaker, journal=journal)
    cue = ThinkingCue(lines=("Hmm.",), after_seconds=30.0)
    task = asyncio.create_task(
        _speak_gated_opener(cue, speaker, journal, opener_turn(runtime, gate, state), gate)
    )
    await asyncio.wait_for(task, 1)

    assert speaker.cues == []
    assert not state.played
    assert journal.events("thinking_cue_skipped")[0]["reason"] == "early_withdrawn"
