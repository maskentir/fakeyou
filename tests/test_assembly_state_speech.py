from typing import cast

import pytest

from fakeyou.config.settings import Settings
from fakeyou.conversation.conversation import MeetingAgent, MeetingResponder
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.conversation.state_speech import render
from fakeyou.conversation.turn_graph import StateSpeech
from fakeyou.foundation.publication import SentenceBinding, StateProvenance
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.orchestration.assembly import (
    DEFAULT_AGENT_WIRING,
    DEFAULT_MODEL_WIRING,
    TurnBehavior,
    _ConversationBuilder,
)
from fakeyou.orchestration.kernel_port import KernelPort
from fakeyou.session.contracts import (
    Observation,
    RosterObserved,
    ScreenObserved,
    SourceHealthObserved,
)
from fakeyou.session.facts import StateQuestion, state_fact
from fakeyou.session.state_publication import state_publication_decision
from tests.test_facts import AYU, AYU_SCREEN, BUDI, IN_CALL, TWIN
from tests.test_state_publication import BUDI_TAKES_OVER

AYU_SHARES: tuple[Observation, ...] = (
    IN_CALL,
    RosterObserved(endpoints=(TWIN, AYU, BUDI, AYU_SCREEN), synced=True),
    ScreenObserved(title="Q4 roadmap"),
)


def observe(kernel: KernelPort, *observations: Observation) -> None:
    for observation in observations:
        kernel.observe("meet_protocol", observation)
    kernel.drain()


def sharing_kernel() -> KernelPort:
    kernel = KernelPort.for_meeting("meeting-9")
    observe(kernel, *AYU_SHARES)
    return kernel


def production_state_speech(settings: Settings, kernel: KernelPort | None) -> StateSpeech:
    builder = _ConversationBuilder(
        settings,
        DEFAULT_AGENT_WIRING,
        TurnBehavior(on_transition=lambda *_args: None, twin_names=("Rani",), kernel=kernel),
        Diagnostics(),
        DEFAULT_MODEL_WIRING,
    )
    responder = MeetingResponder(cast(MeetingAgent, object()), NoHistory())
    speech = builder.build_turns(responder).actor.state_speech
    assert speech is not None
    return speech


def state_binding(provenance: StateProvenance) -> SentenceBinding:
    return SentenceBinding(
        "pub", "meeting", "turn", "turn", "request", 0, "hash", None, (), (), "state", provenance
    )


@pytest.mark.parametrize(
    ("question", "modality", "subject"),
    [
        ("presenter", "participant_claimed", "dev-ayu"),
        ("screen_title", "screen_read", "dev-ayu@"),
    ],
)
def test_production_state_speech_supplies_current_state_provenance(
    settings: Settings, question: StateQuestion, modality: str, subject: str
) -> None:
    kernel = sharing_kernel()

    answer = production_state_speech(settings, kernel)(question, "en")

    assert answer is not None
    fact = state_fact(question, kernel.state)
    assert answer.text == render(fact.proposition, "en")
    provenance = answer.provenance
    assert provenance is not None
    assert provenance.known is True
    assert (provenance.question, provenance.fact_id) == (question, fact.fact_id)
    assert (provenance.namespace, provenance.modality) == ("meeting_live", modality)
    assert (provenance.domain, provenance.revision) == ("screen", kernel.state.revisions.screen)
    assert provenance.valid_from_seq == kernel.state.seq
    assert provenance.subject_id is not None
    assert provenance.subject_id.startswith(subject)


@pytest.mark.parametrize("question", ["presenter", "screen_title"])
def test_production_provenance_is_revalidated_against_the_live_kernel(
    settings: Settings, question: StateQuestion
) -> None:
    kernel = sharing_kernel()
    answer = production_state_speech(settings, kernel)(question, "en")
    assert answer is not None
    assert answer.provenance is not None
    binding = state_binding(answer.provenance)

    unchanged = state_publication_decision(
        binding, kernel.state, now=lambda: 100.0, clock_domain="test"
    )
    observe(kernel, BUDI_TAKES_OVER)
    changed = state_publication_decision(
        binding, kernel.state, now=lambda: 100.0, clock_domain="test"
    )

    assert unchanged.validity == "valid"
    assert changed.validity == "invalid"


@pytest.mark.parametrize("question", ["presenter", "screen_title"])
def test_production_state_speech_marks_an_unknown_fact_as_an_abstention(
    settings: Settings, question: StateQuestion
) -> None:
    kernel = sharing_kernel()
    observe(kernel, SourceHealthObserved(source="meet_protocol", status="stale"))

    answer = production_state_speech(settings, kernel)(question, "en")

    assert answer is not None
    assert answer.provenance is not None
    assert answer.provenance.known is False
    assert answer.provenance.subject_id is None
    assert answer.provenance.source_health == "stale"


@pytest.mark.parametrize("question", ["attendance", "participant_count", "own_action_status"])
def test_production_non_visual_state_answers_carry_no_provenance(
    settings: Settings, question: StateQuestion
) -> None:
    answer = production_state_speech(settings, sharing_kernel())(question, "en")

    assert answer is not None
    assert answer.text
    assert answer.provenance is None


def test_production_state_speech_without_a_kernel_falls_back_to_the_reply(
    settings: Settings,
) -> None:
    assert production_state_speech(settings, None)("presenter", "en") is None
