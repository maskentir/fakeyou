import math
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self

from fakeyou.observability.journal import Journal

PERCEPTION_USD_PER_MTOK = 1.0
IMAGE_TILE_EDGE = 28
PROMPT_TOKEN_ALLOWANCE = 600


def estimate_image_tokens(width: int, height: int) -> int:
    tiles = math.ceil(width / IMAGE_TILE_EDGE) * math.ceil(height / IMAGE_TILE_EDGE)
    return tiles + PROMPT_TOKEN_ALLOWANCE


def tokens_used(usage: Mapping[str, Any] | None, estimate: int) -> int:
    if isinstance(usage, Mapping):
        total = usage.get("total_tokens")
        if isinstance(total, int) and not isinstance(total, bool) and total > 0:
            return total
    return estimate


def usd_for(tokens: int) -> float:
    return tokens * PERCEPTION_USD_PER_MTOK / 1_000_000


class Reservation:
    def __init__(self, budget: "PerceptionBudget", estimate_usd: float) -> None:
        self._budget = budget
        self._estimate_usd = estimate_usd
        self._open = True

    def charge(self, tokens: int) -> float:
        cost = usd_for(tokens)
        if self._open:
            self._open = False
            self._budget.settle(self._estimate_usd, cost)
        return cost

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self._open:
            self._open = False
            self._budget.refund(self._estimate_usd)


class PerceptionBudget:
    def __init__(
        self, *, budget_usd: float, max_calls: int, journal: Journal | None = None
    ) -> None:
        self._budget_usd = budget_usd
        self._max_calls = max_calls
        self._journal = journal or Journal("", echo=False)
        self._spent_usd = 0.0
        self._reserved_usd = 0.0
        self._calls = 0
        self._exhausted_reported = False

    @property
    def spent_usd(self) -> float:
        return self._spent_usd

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def exhausted(self) -> bool:
        return self._calls >= self._max_calls or self._spent_usd >= self._budget_usd

    def reserve(self, estimate_tokens: int, *, operation: str) -> Reservation | None:
        estimate_usd = usd_for(estimate_tokens)
        over_budget = self._spent_usd + self._reserved_usd + estimate_usd > self._budget_usd
        if over_budget or self._calls >= self._max_calls:
            self._report_exhausted(operation)
            return None
        self._reserved_usd += estimate_usd
        self._calls += 1
        return Reservation(self, estimate_usd)

    def settle(self, estimate_usd: float, cost_usd: float) -> None:
        self._reserved_usd -= estimate_usd
        self._spent_usd += cost_usd

    def refund(self, estimate_usd: float) -> None:
        self._reserved_usd -= estimate_usd
        self._calls -= 1

    def _report_exhausted(self, operation: str) -> None:
        if self._exhausted_reported:
            return
        self._exhausted_reported = True
        self._journal.record(
            "budget_exhausted",
            source="perception",
            operation=operation,
            spent_usd=self._spent_usd,
            budget_usd=self._budget_usd,
            calls=self._calls,
        )
