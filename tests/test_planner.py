from dataclasses import dataclass, field

from fakeyou.conversation import turn_graph
from fakeyou.conversation.turn_graph import (
    PlannedTurn,
    TurnRuntime,
    for_reply,
    plan_for,
    plan_notes,
)
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.orchestration.kernel_port import KernelPort
from fakeyou.perception.meet_protocol import MeetUser
from fakeyou.perception.roster import MeetRoster
from fakeyou.session.commands import CommandOutcome, CommandSpec, CommandState
from fakeyou.session.intent import MeetAction, Reading
from fakeyou.session.planner import TurnPlan
from fakeyou.session.policy import Refusal
from tests._persistence_fakes import RecordingJournal


@dataclass
class FakeCommands:
    state: CommandState = "effect_observed"
    ran: list[tuple[CommandSpec, int]] = field(default_factory=list)
    actors: list[str] = field(default_factory=list)

    async def run(
        self, spec: CommandSpec, *, plan_id: str, ordinal: int, actor: str
    ) -> CommandOutcome:
        self.ran.append((spec, ordinal))
        self.actors.append(actor)
        return CommandOutcome(f"{plan_id}:{ordinal}", spec.kind, self.state)


class SilentSpeaker:
    is_speaking = False

    async def speak(self, utterance: str, **_kwargs: object) -> None:
        raise AssertionError(utterance)

    async def stop_playback(self) -> None:
        return None


def event(text: str = "Rani, raise your hand.") -> TranscriptEvent:
    return TranscriptEvent(
        id="t1",
        timestamp="2026-10-27T00:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id="voice:a",
        speaker_evidence_id=None,
    )


def runtime(commands: FakeCommands | None = None) -> TurnRuntime:
    return TurnRuntime(speaker=SilentSpeaker(), journal=RecordingJournal(), commands=commands)


def test_without_planning_no_plan_ever_holds_a_side_effect() -> None:
    reading = Reading("speak", "judge_reply", meet_action="leave", requests_chat=True)

    planned = plan_for(runtime(), event(), reading, "speech")

    assert planned.plan.action is None
    assert planned.plan.leave is False
    assert planned.plan.output == "speech"
    assert planned.actor == "unattributed"


def test_notes_never_report_an_unconfirmed_action_as_done() -> None:
    plan = TurnPlan("p", "action_then_speech", output="speech", action=MeetAction("react", "x"))

    cases: tuple[tuple[CommandState, str], ...] = (
        ("effect_observed", "React is done and the meeting confirmed it."),
        ("effect_unobserved", "do not say anyone saw it"),
        ("unknown", "not sure it went through"),
        ("failed_after_boundary", "React did not happen."),
    )
    for state, expected in cases:
        notes = " ".join(plan_notes(plan, CommandOutcome("c", "react", state)))
        assert expected in notes


def test_refusals_and_leave_are_told_to_the_reply() -> None:
    plan = TurnPlan(
        "p",
        "speech_then_leave",
        output="speech",
        leave=True,
        refusals=(Refusal("raise_hand", "not_permitted"),),
    )

    reply = for_reply(event(), plan, None)

    assert "You are not permitted to raise hand in this meeting" in reply.text
    assert "You will leave the call right after this reply." in reply.text


def test_the_reply_event_carries_only_the_planned_evidence() -> None:
    plan = TurnPlan("p", "evidence_then_speech", output="speech", evidence=("screen",))

    assert for_reply(event("What is on screen?"), plan, None).evidence_needs == ("screen",)


async def test_an_action_only_turn_that_happened_stays_silent() -> None:
    commands = FakeCommands()
    planned = PlannedTurn(TurnPlan("p", "action_only", action=MeetAction("raise_hand")), "dev-a")

    done, outcome = await turn_graph.act_first(runtime(commands), event(), planned)

    assert done is True
    assert outcome is not None
    assert outcome.observed
    assert commands.ran == [(CommandSpec("raise_hand"), 0)]
    assert commands.actors == ["dev-a"]


async def test_an_action_only_turn_that_failed_speaks_a_decline() -> None:
    commands = FakeCommands(state="failed_after_boundary")
    planned = PlannedTurn(TurnPlan("p", "action_only", action=MeetAction("raise_hand")), "dev-a")

    done, outcome = await turn_graph.act_first(runtime(commands), event(), planned)

    assert done is False
    assert outcome is not None
    assert outcome.not_done


async def test_a_leave_only_turn_leaves_without_speaking() -> None:
    commands = FakeCommands(state="effect_unobserved")
    planned = PlannedTurn(TurnPlan("p", "leave_only", leave=True), "dev-a")

    done, _outcome = await turn_graph.act_first(runtime(commands), event(), planned)

    assert done is True
    assert commands.ran == [(CommandSpec("leave"), 1)]


def _joined_kernel() -> KernelPort:
    kernel = KernelPort.for_meeting("meeting-9")
    roster = MeetRoster()
    roster.apply_users(
        [
            MeetUser(device_id="dev-twin", name="Rani", present=True, current_user=True),
            MeetUser(device_id="dev-ayu", name="Ayu", present=True),
        ]
    )
    roster.mark_synced()
    kernel.call_state("in_call")
    kernel.roster(roster)
    return kernel


def test_a_chat_sender_on_the_roster_is_the_actor() -> None:
    kernel = _joined_kernel()
    kernel.chat(
        message_id="m1", device_id="dev-ayu", text="Rani, hand up", own=False, backlog=False
    )
    kernel.chat(
        message_id="m2", device_id="dev-ghost", text="Rani, leave", own=False, backlog=False
    )
    kernel.drain()

    assert kernel.actor("m1").device_id == "dev-ayu"
    assert kernel.actor("m2").kind == "unattributed"
    assert kernel.actor("missing").kind == "unattributed"


class RecordingSpeaker:
    is_speaking = False

    def __init__(self) -> None:
        self.spoken: list[str] = []

    async def speak(self, utterance: str, **_kwargs: object) -> None:
        self.spoken.append(utterance)

    async def stop_playback(self) -> None:
        return None


async def test_a_state_question_is_spoken_from_the_renderer_not_the_model() -> None:
    speaker = RecordingSpeaker()
    answers = {"participant_count": "Besides me, there are 2 people here."}
    planned = PlannedTurn(
        TurnPlan("p", "speech_only", output="speech", state_question="participant_count")
    )
    state_runtime = TurnRuntime(
        speaker=speaker, journal=RecordingJournal(), state_speech=answers.get
    )

    answered = await turn_graph.answer_state(state_runtime, event("How many are here?"), planned, 0)

    assert answered is True
    assert speaker.spoken == ["Besides me, there are 2 people here."]


async def test_without_a_locale_pack_the_state_question_falls_back_to_the_reply() -> None:
    planned = PlannedTurn(TurnPlan("p", "speech_only", output="speech", state_question="presenter"))
    state_runtime = TurnRuntime(
        speaker=RecordingSpeaker(), journal=RecordingJournal(), state_speech=lambda _question: None
    )

    assert await turn_graph.answer_state(state_runtime, event("Who presents?"), planned, 0) is False
