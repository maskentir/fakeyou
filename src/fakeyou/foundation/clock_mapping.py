import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

LOCAL_MONOTONIC = "local_monotonic"
PAGE_CLOCK = "page_wall_utc"
PAGE_CLOCK_EXCHANGE = "page_clock_exchange"


@dataclass(frozen=True, slots=True)
class ClockExchange:
    remote_ms: float
    sent: float
    received: float

    @property
    def valid(self) -> bool:
        return (
            all(math.isfinite(value) for value in (self.remote_ms, self.sent, self.received))
            and self.remote_ms > 0
            and self.sent >= 0
            and self.received >= self.sent
        )

    @property
    def local_midpoint(self) -> float:
        return (self.sent + self.received) / 2

    @property
    def half_round_trip(self) -> float:
        return (self.received - self.sent) / 2


@dataclass(frozen=True, slots=True)
class MappedInstant:
    monotonic: float
    uncertainty_seconds: float
    clock_domain: str = LOCAL_MONOTONIC
    origin: str = PAGE_CLOCK_EXCHANGE


class RemoteClock:
    def __init__(
        self,
        *,
        now: Callable[[], float],
        drift_ppm: float = 200.0,
        max_exchange_age_seconds: float = 30.0,
        max_span_seconds: float = 120.0,
        keep: int = 16,
    ) -> None:
        self._now = now
        self._drift = drift_ppm / 1_000_000
        self._max_exchange_age_seconds = max_exchange_age_seconds
        self._max_span_seconds = max_span_seconds
        self._exchanges: deque[ClockExchange] = deque(maxlen=keep)

    def observe(self, exchange: ClockExchange) -> bool:
        if not exchange.valid:
            return False
        self._exchanges.append(exchange)
        return True

    def map(self, remote_ms: float) -> MappedInstant | None:
        if not math.isfinite(remote_ms) or remote_ms <= 0:
            return None
        now = self._now()
        candidates = [
            self._mapped(exchange, remote_ms)
            for exchange in self._exchanges
            if 0 <= now - exchange.received <= self._max_exchange_age_seconds
            and abs(exchange.remote_ms - remote_ms) / 1000 <= self._max_span_seconds
        ]
        return min(candidates, key=lambda item: item.uncertainty_seconds, default=None)

    def _mapped(self, exchange: ClockExchange, remote_ms: float) -> MappedInstant:
        span = (exchange.remote_ms - remote_ms) / 1000
        return MappedInstant(
            monotonic=exchange.local_midpoint - span,
            uncertainty_seconds=exchange.half_round_trip + self._drift * abs(span),
        )
