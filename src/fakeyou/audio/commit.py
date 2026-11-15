from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fakeyou.foundation.content_digest import ContentSample
from fakeyou.foundation.publication import (
    CAPTURE_FRESHNESS_SECONDS,
    VALIDATION_AGE_SECONDS,
    EvidenceBasis,
    PublicationContext,
    PublicationDecision,
)
from fakeyou.speech.contract import CommitTiming, PostCommitSample

COMMIT_EVALUATIONS = 2
WRITE_DELAYED = "write_delayed_past_validation"
BOUNDED_SCOPES = frozenset({"current", "state"})


class WriteWithheld(RuntimeError):
    pass


@runtime_checkable
class DeadlineWriter(Protocol):
    async def write_before(self, pcm: bytes, deadline: float) -> None: ...


@dataclass(frozen=True, slots=True)
class WriteObservation:
    accepted_at: float
    first_written_at: float
    clock_domain: str


@runtime_checkable
class ObservedWriter(Protocol):
    async def write_observed(self, pcm: bytes, deadline: float) -> WriteObservation: ...


@dataclass(slots=True)
class CommitProgress:
    decision: PublicationDecision | None = None
    evaluated_at: float | None = None
    accepted_at: float | None = None
    first_written_at: float | None = None
    commit_revision: int | None = None
    first_write_revision: int | None = None
    evaluations: int = 0
    bounded: bool = False
    write_completed: bool = False
    writer_clock_domain: str | None = None

    def evaluated(
        self, decision: PublicationDecision, context: PublicationContext, *, bounded: bool
    ) -> float:
        at = context.now()
        self.decision = decision
        self.evaluated_at = at
        self.accepted_at = None
        self.first_written_at = None
        self.write_completed = False
        self.writer_clock_domain = None
        self.commit_revision = None if context.revision is None else context.revision()
        self.evaluations += 1
        self.bounded = bounded
        return at

    def written(
        self, context: PublicationContext, observation: WriteObservation | None = None
    ) -> None:
        self.write_completed = True
        if observation is not None:
            self.writer_clock_domain = observation.clock_domain
            if observation.clock_domain == context.clock_domain:
                self.accepted_at = observation.accepted_at
                self.first_written_at = observation.first_written_at
        self.first_write_revision = None if context.revision is None else context.revision()

    def timing(self, context: PublicationContext) -> CommitTiming | None:
        if self.evaluated_at is None:
            return None
        return CommitTiming(
            evaluated_at=self.evaluated_at,
            accepted_at=self.accepted_at,
            first_written_at=self.first_written_at,
            clock_domain=context.clock_domain,
            commit_revision=self.commit_revision,
            evaluations=self.evaluations,
            fresh_at_write_unproven=self._unproven(),
            post_commit=self._post_commit(context),
            writer_clock_domain=self.writer_clock_domain,
        )

    def _unproven(self) -> bool:
        written = self.first_written_at
        evaluated = self.evaluated_at
        accepted = self.accepted_at
        if not self.bounded:
            return False
        if written is None or evaluated is None:
            return self.write_completed
        if accepted is None or not math.isfinite(accepted):
            return True
        if not evaluated <= accepted <= written:
            return True
        if not (math.isfinite(written) and math.isfinite(evaluated)) or written < evaluated:
            return True
        if written - evaluated > VALIDATION_AGE_SECONDS:
            return True
        decision = self.decision
        return decision is None or not all(_fresh_at(item, written) for item in decision.basis)

    def deadline(self) -> float:
        evaluated = self.evaluated_at
        if evaluated is None:
            return float("-inf")
        deadlines = [evaluated + VALIDATION_AGE_SECONDS]
        if self.decision is not None and self.bounded:
            for basis in self.decision.basis:
                sample = basis.confirmation if basis.match == "exact" else basis.analyzed
                capture = None if sample is None else sample.capture
                if capture is not None:
                    deadlines.append(
                        capture.monotonic + CAPTURE_FRESHNESS_SECONDS - capture.uncertainty_seconds
                    )
        return min(deadlines)

    def _post_commit(self, context: PublicationContext) -> tuple[PostCommitSample, ...]:
        revision = self.commit_revision
        if revision is None or context.samples_since is None:
            return ()
        return tuple(self._position(sample, context) for sample in context.samples_since(revision))

    def _position(self, sample: ContentSample, context: PublicationContext) -> PostCommitSample:
        capture = sample.capture
        comparable = capture is not None and capture.clock_domain == context.clock_domain
        evaluated = self.evaluated_at
        written = self.first_write_revision
        return PostCommitSample(
            role=sample.record.role,
            subject_id=sample.record.subject_id,
            sequence=sample.record.sequence,
            captured_at=capture.monotonic if capture is not None and comparable else None,
            before_first_write=written is None or sample.revision <= written,
            captured_before_commit=(
                capture.monotonic < evaluated
                if capture is not None and comparable and evaluated is not None
                else None
            ),
        )


def _fresh_at(basis: EvidenceBasis, at: float) -> bool:
    sample = basis.confirmation if basis.match == "exact" else basis.analyzed
    capture = None if sample is None else sample.capture
    if capture is None:
        return False
    return at - capture.monotonic + capture.uncertainty_seconds <= CAPTURE_FRESHNESS_SECONDS
