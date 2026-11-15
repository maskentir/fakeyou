import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fakeyou.foundation.clock import monotonic
from fakeyou.foundation.clock_mapping import LOCAL_MONOTONIC
from fakeyou.foundation.publication import (
    EvidenceBinding,
    PublicationContext,
    PublicationDecision,
    SentenceBinding,
    Validation,
    decide_visual_publication,
    evaluate_visual_publication,
)
from fakeyou.orchestration.kernel_port import KernelPort
from fakeyou.perception.context import PerceptionContext
from fakeyou.perception.revalidation import SceneRevalidator
from fakeyou.session.state_publication import state_publication_decision
from fakeyou.session.world import SessionState

Revalidate = Callable[[EvidenceBinding], Awaitable[Validation]]
PublicationGate = Callable[[SentenceBinding], Awaitable[PublicationDecision | None]]
PublicationCommit = Callable[[SentenceBinding], PublicationDecision | None]


@dataclass(frozen=True, slots=True)
class GateSources:
    revalidate: Revalidate | None = None
    state: Callable[[], SessionState] | None = None
    validate: Callable[[EvidenceBinding], Validation] | None = None


def compose_publication_gate(
    sources: GateSources,
    *,
    now: Callable[[], float] = monotonic,
    clock_domain: str = LOCAL_MONOTONIC,
) -> PublicationGate:
    async def gate(binding: SentenceBinding) -> PublicationDecision | None:
        if binding.scope == "state":
            if sources.state is None:
                return None
            return state_publication_decision(
                binding, sources.state(), now=now, clock_domain=clock_domain
            )
        if binding.scope == "current" and sources.revalidate is None:
            return None
        return await evaluate_visual_publication(
            binding, sources.revalidate or _never, now=now, clock_domain=clock_domain
        )

    return gate


def compose_publication_commit(
    sources: GateSources,
    *,
    now: Callable[[], float] = monotonic,
    clock_domain: str = LOCAL_MONOTONIC,
) -> PublicationCommit:
    def commit(binding: SentenceBinding) -> PublicationDecision | None:
        if binding.scope == "state":
            if sources.state is None:
                return None
            return state_publication_decision(
                binding, sources.state(), now=now, clock_domain=clock_domain
            )
        validate = sources.validate
        if binding.scope == "current" and validate is None:
            return None
        validations = (
            {} if validate is None else {item.kind: validate(item) for item in binding.evidence}
        )
        return decide_visual_publication(binding, validations, at=now(), clock_domain=clock_domain)

    return commit


async def _never(evidence: EvidenceBinding) -> Validation:
    raise RuntimeError(f"no revalidator for {evidence.kind}")


def opt_in_publication_context(
    meeting_id: str,
    *,
    perception: PerceptionContext | None,
    kernel: KernelPort | None,
    now: Callable[[], float] = monotonic,
    id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> PublicationContext:
    revalidator = None if perception is None else SceneRevalidator(perception, now=now)
    sources = GateSources(
        revalidate=revalidator,
        state=None if kernel is None else lambda: kernel.state,
        validate=None if revalidator is None else revalidator.validate,
    )
    samples = None if perception is None else perception.timeline.samples
    return PublicationContext(
        meeting_id=meeting_id,
        clock_domain=LOCAL_MONOTONIC,
        gate=compose_publication_gate(sources, now=now),
        now=now,
        id_factory=id_factory,
        commit=compose_publication_commit(sources, now=now),
        revision=None if samples is None else lambda: samples.revision,
        samples_since=None if samples is None else samples.since,
    )
