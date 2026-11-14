import hashlib
from collections.abc import Callable

from fakeyou.foundation.publication import (
    PublicationDecision,
    SentenceBinding,
    StateProvenance,
    evaluate_state_publication,
)
from fakeyou.session.facts import Fact, StateQuestion, Unknown, state_fact
from fakeyou.session.world import SessionState, presenter_device

CURRENT_STATE_QUESTIONS: frozenset[StateQuestion] = frozenset({"presenter", "screen_title"})


def _subject(question: StateQuestion, state: SessionState) -> str | None:
    device = presenter_device(state.world)
    screen = state.world.screen
    if question != "screen_title" or device is None or screen is None:
        return device
    return f"{device}@{screen.since_seq}"


def state_provenance(question: StateQuestion, fact: Fact, state: SessionState) -> StateProvenance:
    known = not isinstance(fact.proposition, Unknown)
    return StateProvenance(
        question=question,
        fact_id=fact.fact_id,
        namespace=fact.namespace,
        modality=fact.modality,
        domain=fact.domain,
        revision=fact.revision,
        valid_from_seq=fact.valid_from_seq,
        subject_id=_subject(question, state) if known else None,
        claim=hashlib.sha256(repr(fact.proposition).encode()).hexdigest(),
        known=known,
        source_health=state.health.sources.get("meet_protocol"),
    )


def latest_state_provenance(
    claimed: StateProvenance, state: SessionState
) -> StateProvenance | None:
    question = next((item for item in CURRENT_STATE_QUESTIONS if item == claimed.question), None)
    if question is None:
        return None
    return state_provenance(question, state_fact(question, state), state)


def state_publication_decision(
    binding: SentenceBinding,
    state: SessionState,
    *,
    now: Callable[[], float],
    clock_domain: str,
) -> PublicationDecision:
    return evaluate_state_publication(
        binding,
        lambda claimed: latest_state_provenance(claimed, state),
        now=now,
        clock_domain=clock_domain,
    )
