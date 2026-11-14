from dataclasses import dataclass, field, replace
from typing import Any

from fakeyou.foundation.clock_mapping import ClockExchange, MappedInstant, RemoteClock
from fakeyou.foundation.publication import SentenceBinding
from fakeyou.observability.participant_audio import HeardSentence, ParticipantConsent

OBSERVER_ID = "observer-1"
OBSERVER_EPOCH_MS = 1_000_000.0


def granted(at: float = 10.0, **changes: Any) -> ParticipantConsent:
    consent = ParticipantConsent(OBSERVER_ID, "granted", "participant_confirmed", granted_at=at)
    return replace(consent, **changes)


def observer_clock(
    *, local_at_epoch: float = 0.0, round_trip: float = 0.02, now: float = 200.0
) -> RemoteClock:
    clock = RemoteClock(now=lambda: now, drift_ppm=0, max_span_seconds=1000)
    midpoint = local_at_epoch + 190.0
    assert clock.observe(
        ClockExchange(
            OBSERVER_EPOCH_MS + 190_000, midpoint - round_trip / 2, midpoint + round_trip / 2
        )
    )
    return clock


def observer_ms(local_seconds: float) -> float:
    return OBSERVER_EPOCH_MS + local_seconds * 1000


@dataclass
class ScriptedParticipantObserver:
    consented: ParticipantConsent = field(default_factory=granted)
    clock: RemoteClock | None = field(default_factory=observer_clock)
    observer_id: str = OBSERVER_ID
    heard_from: float | None = 100.0
    heard_until: float = 101.0
    identity_method: str = "watermark"
    reported_hash: str | None = None
    reported_by: str | None = None
    heard_calls: int = 0

    def consent(self) -> ParticipantConsent:
        return self.consented

    def heard(self, binding: SentenceBinding) -> HeardSentence | None:
        self.heard_calls += 1
        if self.heard_from is None:
            return None
        return HeardSentence(
            observer_id=self.reported_by or self.observer_id,
            publication_id=binding.publication_id,
            transcript_id=binding.transcript_id,
            sentence_index=binding.sentence_index,
            sentence_hash=self.reported_hash or binding.sentence_hash,
            identity_method=self.identity_method,
            started_ms=observer_ms(self.heard_from),
            ended_ms=observer_ms(self.heard_until),
        )

    def instant(self, observer_ms: float) -> MappedInstant | None:
        if self.clock is None:
            return None
        mapped = self.clock.map(observer_ms)
        if mapped is None:
            return None
        return MappedInstant(mapped.monotonic, mapped.uncertainty_seconds, "fake", mapped.origin)
