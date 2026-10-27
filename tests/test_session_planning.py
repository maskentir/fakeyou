from itertools import product
from typing import get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fakeyou.session.commands import (
    TERMINAL_COMMAND_STATES,
    CommandKind,
    CommandSpec,
    CommandState,
    CommandTransitionError,
    command_id,
    recovered,
    transition,
)
from fakeyou.session.intent import (
    Answer,
    ChatRequest,
    Clarify,
    Compound,
    Intent,
    MeetAction,
    NoResponse,
    Reading,
    intent_of,
)
from fakeyou.session.planner import PlanTransitionError, TurnPlan, advance, plan_turn
from fakeyou.session.policy import (
    UNATTRIBUTED,
    Actor,
    Channel,
    Grant,
    Permission,
    authorize,
)

AYU = Actor("participant", "dev-ayu")
EVERYTHING: frozenset[Permission] = frozenset(get_args(Permission))


def test_a_silent_reading_is_no_response() -> None:
    assert intent_of(Reading("silent", "judge_not_addressed")) == NoResponse("judge_not_addressed")


def test_an_action_only_request_is_just_the_action() -> None:
    reading = Reading("speak", "judge_reply", meet_action="raise_hand", action_only=True)

    assert intent_of(reading) == MeetAction("raise_hand")


def test_a_reaction_without_a_glyph_is_not_an_action() -> None:
    reading = Reading("speak", "judge_reply", meet_action="react", action_only=True)

    assert intent_of(reading) == Answer()


def test_an_action_with_an_answer_is_one_action_and_one_output() -> None:
    reading = Reading(
        "speak",
        "judge_reply",
        meet_action="react",
        reaction_emoji="\U0001f44d",
        evidence=("screen", "memory", "chat_history", "bogus"),
    )

    assert intent_of(reading) == Compound(
        MeetAction("react", "\U0001f44d"), Answer(("screen", "chat_history"))
    )


def test_a_chat_request_never_becomes_action_only() -> None:
    reading = Reading(
        "speak", "judge_reply", meet_action="mute", action_only=True, requests_chat=True
    )

    assert intent_of(reading) == Compound(MeetAction("mute"), ChatRequest())


def test_an_unattributed_leave_request_is_refused_and_answered() -> None:
    authorization = authorize(
        MeetAction("leave"), actor=UNATTRIBUTED, channel="speech", grant=Grant(EVERYTHING)
    )

    assert authorization.intent == Answer()
    assert [refusal.reason for refusal in authorization.refusals] == ["unattributed_actor"]


def test_leave_needs_its_own_permission() -> None:
    lightweight = Grant(frozenset({"meet_act", "meet_chat"}))

    refused = authorize(MeetAction("leave"), actor=AYU, channel="speech", grant=lightweight)
    allowed = authorize(MeetAction("raise_hand"), actor=AYU, channel="speech", grant=lightweight)

    assert refused.refusals[0].reason == "not_permitted"
    assert allowed.intent == MeetAction("raise_hand")


def test_a_refused_action_keeps_the_requested_output() -> None:
    intent = Compound(MeetAction("react", "\U0001f44d"), Answer(("screen",)))

    authorization = authorize(intent, actor=AYU, channel="speech", grant=Grant(frozenset()))

    assert authorization.intent == Answer(("screen",))


def test_a_chat_request_without_chat_permission_is_spoken() -> None:
    authorization = authorize(
        ChatRequest(), actor=AYU, channel="speech", grant=Grant(frozenset({"meet_act"}))
    )

    assert authorization.intent == Answer()
    assert authorization.refusals[0].reason == "chat_not_permitted"


def test_a_chat_channel_turn_without_chat_permission_stays_silent() -> None:
    authorization = authorize(Answer(), actor=AYU, channel="chat", grant=Grant(frozenset()))

    assert authorization.intent == NoResponse("chat_not_permitted")


def test_an_unknown_browser_refuses_every_side_effect() -> None:
    grant = Grant(EVERYTHING, browser_available=False)

    action = authorize(MeetAction("mute"), actor=AYU, channel="speech", grant=grant)
    chat = authorize(ChatRequest(), actor=AYU, channel="speech", grant=grant)

    assert action.intent == Answer()
    assert chat.intent == Answer()
    assert {refusal.reason for refusal in (*action.refusals, *chat.refusals)} == {
        "browser_unavailable"
    }


intents = st.sampled_from(
    [
        NoResponse("x"),
        Answer(),
        Answer(("screen",)),
        Clarify(),
        ChatRequest(),
        MeetAction("raise_hand"),
        MeetAction("react", "\U0001f44d"),
        MeetAction("leave"),
        Compound(MeetAction("mute"), Answer()),
        Compound(MeetAction("leave"), Answer()),
        Compound(MeetAction("leave"), ChatRequest()),
        Compound(MeetAction("react", "\U0001f44d"), ChatRequest()),
    ]
)
actors = st.sampled_from([AYU, UNATTRIBUTED])
channels = st.sampled_from(["speech", "chat"])
grants = st.builds(
    Grant,
    st.sets(st.sampled_from(get_args(Permission))).map(frozenset),
    browser_available=st.booleans(),
)


def _side_effects(plan: TurnPlan) -> set[str]:
    effects: set[str] = set()
    if plan.action is not None:
        effects.add(plan.action.kind)
    if plan.leave:
        effects.add("leave")
    if plan.output == "chat":
        effects.add("send_chat")
    return effects


@given(intents, actors, channels, grants)
def test_no_plan_holds_a_side_effect_its_grant_does_not_allow(
    intent: Intent,
    actor: Actor,
    channel: Channel,
    grant: Grant,
) -> None:
    plan = plan_turn(
        authorize(intent, actor=actor, channel=channel, grant=grant),
        channel=channel,
        plan_id="p1",
    )

    for effect in _side_effects(plan):
        assert grant.browser_available
        assert actor.kind == "participant"
        needed = {"leave": "meet_leave", "send_chat": "meet_chat"}.get(effect, "meet_act")
        assert needed in grant.permissions
    assert len(plan.evidence) <= 2


@pytest.mark.parametrize(
    ("intent", "channel", "template"),
    [
        (NoResponse("x"), "speech", "silent"),
        (Answer(), "speech", "speech_only"),
        (Answer(("screen",)), "speech", "evidence_then_speech"),
        (Answer(("memory",)), "chat", "evidence_then_chat"),
        (ChatRequest(), "speech", "chat_only"),
        (Clarify(), "speech", "speech_only"),
        (MeetAction("raise_hand"), "speech", "action_only"),
        (MeetAction("leave"), "speech", "leave_only"),
        (Compound(MeetAction("mute"), Answer()), "speech", "action_then_speech"),
        (Compound(MeetAction("leave"), Answer()), "speech", "speech_then_leave"),
        (Compound(MeetAction("leave"), Answer()), "chat", "chat_then_leave"),
        (Compound(MeetAction("mute"), ChatRequest()), "speech", "speech_only"),
    ],
)
def test_every_intent_lands_on_one_finite_template(
    intent: Intent, channel: Channel, template: str
) -> None:
    grant = Grant(EVERYTHING)
    plan = plan_turn(
        authorize(intent, actor=AYU, channel=channel, grant=grant), channel=channel, plan_id="p"
    )

    assert plan.template == template


def test_an_action_with_a_chat_reply_has_no_template_and_asks_instead() -> None:
    plan = plan_turn(
        authorize(
            Compound(MeetAction("mute"), ChatRequest()),
            actor=AYU,
            channel="speech",
            grant=Grant(EVERYTHING),
        ),
        channel="speech",
        plan_id="p",
    )

    assert plan.clarify is True
    assert plan.action is None


def test_a_plan_moves_forward_or_ends_but_never_back() -> None:
    state = advance("proposed", "authorized")
    state = advance(state, "waiting")

    assert advance(state, "superseded") == "superseded"
    with pytest.raises(PlanTransitionError):
        advance(state, "authorized")
    with pytest.raises(PlanTransitionError):
        advance("waiting", "completed")
    with pytest.raises(PlanTransitionError):
        advance("completed", "cancelled")


def test_a_command_id_is_stable_for_the_same_plan_step() -> None:
    spec = CommandSpec("send_chat", "Deck is in the drive.")

    assert command_id("a1", "p1", 0, spec) == command_id("a1", "p1", 0, spec)
    assert command_id("a1", "p1", 0, spec) != command_id("a2", "p1", 0, spec)
    assert command_id("a1", "p1", 0, spec) != command_id("a1", "p1", 1, spec)


@pytest.mark.parametrize("kind", ["send_chat", "react", "leave"])
def test_only_toggles_may_be_retried_after_the_boundary(kind: CommandKind) -> None:
    with pytest.raises(CommandTransitionError):
        transition(kind, "dispatching", "dispatching")
    with pytest.raises(CommandTransitionError):
        transition("mute", "dispatching", "ready")
    assert transition("mute", "dispatching", "dispatching") == "dispatching"


def test_a_terminal_command_never_changes() -> None:
    for state in TERMINAL_COMMAND_STATES:
        with pytest.raises(CommandTransitionError):
            transition("raise_hand", state, "effect_observed")


def test_recovery_never_resumes_a_command() -> None:
    states: tuple[CommandState, ...] = get_args(CommandState)

    recovered_states = {state: recovered(state) for state in states}

    assert recovered_states["dispatching"] == "unknown"
    assert recovered_states["accepted"] == "cancelled_before_boundary"
    assert recovered_states["ready"] == "cancelled_before_boundary"
    assert all(state in TERMINAL_COMMAND_STATES for state in recovered_states.values())


def test_verification_strength_follows_the_effect() -> None:
    strengths = {
        kind: CommandSpec(kind).verification for kind, _ in product(get_args(CommandKind), [None])
    }

    assert strengths == {
        "raise_hand": "readback",
        "lower_hand": "readback",
        "mute": "readback",
        "unmute": "readback",
        "react": "acted_only",
        "send_chat": "protocol_echo",
        "leave": "acted_only",
    }
