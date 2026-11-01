import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast, override

from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from fakeyou.conversation.actor import ActorWiring, FloorActor, IngressOptions
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.session import MeetingAudio
from fakeyou.conversation.turn_graph import build_turn_graph
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.speech.contract import PreparedSpeech


class Journal:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.fields: list[dict[str, Any]] = []

    def record(self, event: str, *, source: str, **fields: Any) -> None:
        self.names.append(event)
        self.fields.append({"source": source, **fields})


class Responder:
    async def stream_reply(self, transcript: str) -> AsyncIterator[SpokenChunk]:
        yield SpokenChunk(text=transcript, language="en")


class Speaker:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.is_speaking = False

    async def speak(self, utterance: str, **_: Any) -> None:
        self.spoken.append(utterance)

    async def stop_playback(self) -> None:
        self.is_speaking = False

    async def prepare(self, utterance: str, **_: Any) -> PreparedSpeech | None:
        del utterance
        return None

    async def speak_prepared(self, prepared: PreparedSpeech) -> None:
        del prepared

    async def play_cue(self, text: str) -> bool:
        del text
        return False


def transcript(event_id: str, text: str) -> TranscriptEvent:
    return TranscriptEvent(
        id=event_id,
        timestamp="2026-11-01T00:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id="p1",
        speaker_evidence_id=None,
    )


async def wait_forever() -> None:
    await asyncio.Event().wait()


class ActorModel(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.runner = asyncio.Runner()
        self.journal = Journal()
        self.speaker = Speaker()
        self.actor = FloorActor(
            build_turn_graph(),
            ActorWiring(
                journal=self.journal,
                responder=Responder(),
                ingress=IngressOptions(addressing_names=("twin",)),
            ),
        )
        self.actor.bind(cast(MeetingAudio, self.speaker))
        self.held: asyncio.Task[bool] | None = None
        self.turns = 0

    def run(self, awaitable: Any) -> Any:
        return self.runner.run(awaitable)

    @rule()
    def take_the_floor(self) -> None:
        if self.held is not None and not self.held.done():
            return
        self.turns += 1
        self.held = self.runner.get_loop().create_task(self.actor.floor.hold(wait_forever()))
        self.run(asyncio.sleep(0))

    @rule()
    def release_the_floor(self) -> None:
        if self.held is None:
            return
        self.run(self.actor.floor.interrupt("released"))
        self.held = None

    @rule()
    def a_final_arrives(self) -> None:
        self.run(self.actor.handle_final(transcript(f"f{self.turns}", "twin, are you there?")))
        self.turns += 1

    @rule()
    def a_chat_message_arrives(self) -> None:
        self.run(self._post_chat_message())
        self.turns += 1

    async def _post_chat_message(self) -> bool:
        return self.actor.receive_chat(
            transcript(f"c{self.turns}", "twin, are you listening in chat?")
        )

    @rule()
    def an_attention_call_arrives(self) -> None:
        self.run(self._post_attention_call())
        self.turns += 1

    async def _post_attention_call(self) -> bool:
        return self.actor.receive_attention(
            transcript(f"a{self.turns}", "twin, look at this right now"), channel="chat"
        )

    @rule()
    def an_interim_arrives(self) -> None:
        self.run(self.actor.handle_interim("twin, please stop talking now"))

    @rule()
    def the_room_becomes_active(self) -> None:
        self.actor.note_activity()

    @rule()
    def the_session_pauses(self) -> None:
        self.run(self.actor.pause())

    @rule()
    def the_session_resumes(self) -> None:
        self.actor.resume()

    @invariant()
    def one_turn_holds_the_floor(self) -> None:
        if self.held is not None and not self.held.done():
            assert self.actor.floor.busy

    @invariant()
    def a_paused_actor_takes_no_ingress(self) -> None:
        if self.actor.is_paused:
            assert not self.actor.accepts_ingress

    @invariant()
    def the_journal_never_reports_a_second_active_turn(self) -> None:
        started = self.journal.names.count("agent_started")
        finished = self.journal.names.count("agent_finished") + self.journal.names.count(
            "agent_cancelled"
        )
        assert started - finished <= 1

    @override
    def teardown(self) -> None:
        if self.held is not None:
            self.held.cancel()
            self.run(self._settle_held())
        self.run(self.actor.close())
        self.runner.close()

    async def _settle_held(self) -> None:
        if self.held is not None:
            await asyncio.gather(self.held, return_exceptions=True)


TestActor = ActorModel.TestCase
TestActor.settings = settings(max_examples=100, stateful_step_count=25, deadline=None)
