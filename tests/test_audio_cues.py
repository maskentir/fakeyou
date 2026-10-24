import asyncio

from fakeyou.audio.audio import MeetingAudioSession, SessionCapture
from fakeyou.audio.own_speech import FinalGate, OwnSpeech
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.provider_faults import ProviderFault
from tests._audio_fakes import FakeReconnectableSTT
from tests._audio_support import build_run, wait_gate
from tests._persistence_fakes import RecordingJournal


class CueEngine:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.prepared: list[str] = []
        self.played: list[str] = []
        self.speaking_while_cued: list[bool] = []
        self.gate: FinalGate | None = None

    @property
    def is_speaking(self) -> bool:
        return False

    def louder(self) -> None:
        pass

    async def speak(
        self, text: str, *, sentence_index: int | None = None, transcript_id: str | None = None
    ) -> None:
        del text, sentence_index, transcript_id

    async def stop(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def prepare_cue(self, text: str) -> None:
        if self.failure is not None:
            raise self.failure
        self.prepared.append(text)

    async def play_cue(self, text: str) -> bool:
        assert self.gate is not None
        self.speaking_while_cued.append(self.gate.own_speech.heard_back(text))
        self.played.append(text)
        return True


async def cue_session(
    engine: CueEngine, faults: FaultReporter | None = None
) -> tuple[MeetingAudioSession, asyncio.Event, asyncio.Task[None]]:
    gate = asyncio.Event()
    runner: asyncio.Task[None] = asyncio.create_task(wait_gate(gate))
    final_gate = FinalGate(OwnSpeech())
    engine.gate = final_gate
    session = MeetingAudioSession(
        build_run(engine, runner),
        capture=SessionCapture(stt=FakeReconnectableSTT(), gate=final_gate),
        diagnostics=Diagnostics(faults=faults),
    )
    return session, gate, runner


async def test_cues_are_prepared_in_the_background_and_played_as_own_speech() -> None:
    engine = CueEngine()
    session, gate, runner = await cue_session(engine)

    session.prepare_cues(("Hmm…", "Hmm, one sec…"))
    session.prepare_cues(("ignored",))
    await asyncio.sleep(0)
    played = await session.play_cue("Hmm, one sec…")
    gate.set()
    await runner

    assert engine.prepared == ["Hmm…", "Hmm, one sec…"]
    assert played is True
    assert engine.played == ["Hmm, one sec…"]
    assert engine.speaking_while_cued == [True]


async def test_a_cue_that_cannot_be_synthesized_is_a_fault_not_a_failed_meeting() -> None:
    faults = FaultReporter(journal=RecordingJournal())
    engine = CueEngine(failure=ProviderFault("tts", "unavailable", retryable=True))
    session, gate, runner = await cue_session(engine, faults)

    session.prepare_cues(("Hmm…",))
    await asyncio.sleep(0)
    session.raise_if_failed()
    gate.set()
    await runner

    assert engine.prepared == []
    assert faults.total() == 1
