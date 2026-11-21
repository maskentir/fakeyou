import asyncio
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from evals._lineage_classifier import Pricing, spend

_STRICT = ConfigDict(extra="forbid")
MODEL_ROUTE = re.compile(r"^/v1beta/models/(?P<model>[^:/]+):")
SSE_DATA = re.compile(r"^data: (?P<json>.+)$", re.MULTILINE)

StopReason = Literal[
    "attempt_limit",
    "spend_limit",
    "unknown_pricing",
    "provider_failure",
    "usage_unknown",
]


class RecordingCaps(BaseModel):
    model_config = _STRICT

    max_attempts: Annotated[int, Field(ge=1)]
    max_spend_usd: Annotated[float, Field(gt=0)]
    max_output_tokens_per_call: Annotated[int, Field(ge=1)]


class RecordedAttempt(BaseModel):
    model_config = _STRICT

    route: str
    status: int
    reserved_usd: float
    input_tokens: int | None
    output_tokens: int | None
    charged_usd: float


class RecordingLedger(BaseModel):
    model_config = _STRICT

    caps: RecordingCaps
    pricing: dict[str, Pricing]
    attempts: list[RecordedAttempt] = Field(default_factory=list)
    stop_reason: StopReason | None = None
    stop_detail: str | None = None

    @property
    def spend_usd(self) -> float:
        return sum(attempt.charged_usd for attempt in self.attempts)

    def report(self) -> dict[str, object]:
        return {
            "attempts": len(self.attempts),
            "failed_attempts": sum(not 200 <= item.status < 300 for item in self.attempts),
            "input_tokens": sum(item.input_tokens or 0 for item in self.attempts),
            "output_tokens": sum(item.output_tokens or 0 for item in self.attempts),
            "estimated_spend_usd": round(self.spend_usd, 6),
            "caps": self.caps.model_dump(),
            "stop_reason": self.stop_reason,
            "stop_detail": self.stop_detail,
        }


class RecordingStopped(RuntimeError):
    pass


def usage_tokens(body: str) -> tuple[int, int] | None:
    payloads = [match["json"] for match in SSE_DATA.finditer(body)] or [body]
    usage: Mapping[str, Any] | None = None
    for payload in payloads:
        try:
            parsed = json.loads(payload)
        except ValueError:
            return None
        if isinstance(parsed, dict) and isinstance(parsed.get("usageMetadata"), dict):
            usage = parsed["usageMetadata"]
    if usage is None or not isinstance(usage.get("promptTokenCount"), int):
        return None
    prompt = int(usage["promptTokenCount"]) + int(usage.get("toolUsePromptTokenCount", 0))
    output = int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0))
    return prompt, output


@dataclass(slots=True)
class RecordingBudget:
    path: Path
    ledger: RecordingLedger
    in_flight: list[float] = field(default_factory=list)
    _settled: asyncio.Condition = field(default_factory=asyncio.Condition)

    @classmethod
    def load(cls, path: Path) -> "RecordingBudget":
        return cls(path, RecordingLedger.model_validate_json(path.read_text(encoding="utf-8")))

    def _save(self) -> None:
        self.path.write_text(self.ledger.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def _stop(self, reason: StopReason, detail: str) -> RecordingStopped:
        if self.ledger.stop_reason is None:
            self.ledger.stop_reason = reason
            self.ledger.stop_detail = detail
            self._save()
        return RecordingStopped(f"recording stopped: {self.ledger.stop_reason}")

    def _price(self, route: str) -> Pricing | None:
        model = MODEL_ROUTE.match(route)
        return self.ledger.pricing.get(model["model"]) if model else None

    async def admit(self, route: str, body: bytes) -> float:
        async with self._settled:
            while True:
                if self.ledger.stop_reason is not None:
                    raise self._stop(self.ledger.stop_reason, "")
                caps = self.ledger.caps
                if len(self.ledger.attempts) + len(self.in_flight) >= caps.max_attempts:
                    raise self._stop("attempt_limit", f"{caps.max_attempts} attempts used")
                price = self._price(route)
                if price is None:
                    raise self._stop("unknown_pricing", route)
                reservation = spend(price, len(body), caps.max_output_tokens_per_call)
                if self.ledger.spend_usd + sum(self.in_flight) + reservation <= caps.max_spend_usd:
                    self.in_flight.append(reservation)
                    return reservation
                if not self.in_flight:
                    raise self._stop(
                        "spend_limit",
                        f"{reservation:.6f} USD reservation does not fit the remaining budget",
                    )
                await self._settled.wait()

    async def settle(self, route: str, reservation: float, status: int, body: str) -> None:
        async with self._settled:
            self.in_flight.remove(reservation)
            tokens = usage_tokens(body) if 200 <= status < 300 else None
            price = self._price(route)
            charged = (
                spend(price, *tokens) if tokens is not None and price is not None else reservation
            )
            self.ledger.attempts.append(
                RecordedAttempt(
                    route=route,
                    status=status,
                    reserved_usd=reservation,
                    input_tokens=tokens[0] if tokens else None,
                    output_tokens=tokens[1] if tokens else None,
                    charged_usd=charged,
                )
            )
            self._save()
            if not 200 <= status < 300:
                self._stop("provider_failure", f"{route} answered {status}")
            elif tokens is None:
                self._stop("usage_unknown", f"{route} answered without usage metadata")
            self._settled.notify_all()
