import asyncio
import re
from dataclasses import dataclass
from typing import get_args

import httpx2
from openai import APIConnectionError, APIStatusError, APITimeoutError

from fakeyou.catalog.errors import FakeYouError
from fakeyou.foundation.taxonomy import ProviderFaultKind, Subsystem

ERROR_CATEGORY_KINDS: dict[str, tuple[ProviderFaultKind, bool]] = {
    "AUTHENTICATION": ("unauthorized", False),
    "AUTHORIZATION": ("unauthorized", False),
    "QUOTA": ("payment_required", False),
    "RATE_LIMIT": ("rate_limited", True),
    "CONNECTIVITY": ("unavailable", True),
    "SERVER": ("unavailable", True),
    "INVALID_REQUEST": ("bad_request", False),
    "APPLICATION": ("unknown", False),
    "UNKNOWN": ("unknown", False),
}

MAX_DETAIL_CHARS = 300
_FAULT_KINDS = frozenset(get_args(ProviderFaultKind))
_SECRET = re.compile(r"(?i)(bearer\s+|api[_-]?key[=:]\s*|token[=:]\s*)[^\s,;]+")
_URL = re.compile(r"https?://[^\s]+")
_JSON_SECRET = re.compile(
    r'(?i)("(?:api[_-]?key|authorization|token|password|secret|body)"\s*:\s*)[^,}]+'
)


def trim_detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    safe = _URL.sub("<url>", detail)
    safe = _SECRET.sub(r"\1<redacted>", safe)
    safe = _JSON_SECRET.sub(r"\1<redacted>", safe)
    return safe[:MAX_DETAIL_CHARS] or None


@dataclass(frozen=True, slots=True)
class FaultEvidence:
    status: int | None = None
    detail: str | None = None


class ProviderFault(Exception):
    def __init__(
        self,
        source: Subsystem,
        kind: ProviderFaultKind,
        *,
        evidence: FaultEvidence | None = None,
        retryable: bool = False,
        operation: str | None = None,
    ) -> None:
        self.source: Subsystem = source
        self.operation = operation
        self.kind = kind if kind in _FAULT_KINDS else "unknown"
        seen = FaultEvidence() if evidence is None else evidence
        self.status = (
            seen.status
            if isinstance(seen.status, int) and not isinstance(seen.status, bool)
            else None
        )
        self.detail = trim_detail(seen.detail)
        self.retryable = bool(retryable)
        message = f"{self.source} {self.kind}"
        if self.operation:
            message = f"{self.source}.{self.operation} {self.kind}"
        if self.status is not None:
            message = f"{message} (status {self.status})"
        if self.detail:
            message = f"{message}: {self.detail}"
        super().__init__(message)


def classify_http(source: Subsystem, status: int, detail: str | None = None) -> ProviderFault:
    evidence = FaultEvidence(status, detail)
    if status == 402:
        return ProviderFault(source, "payment_required", evidence=evidence)
    if status in (401, 403):
        return ProviderFault(source, "unauthorized", evidence=evidence)
    if status == 429:
        return ProviderFault(source, "rate_limited", evidence=evidence, retryable=True)
    if 500 <= status <= 599:
        return ProviderFault(source, "unavailable", evidence=evidence, retryable=True)
    if 400 <= status <= 499:
        return ProviderFault(source, "bad_request", evidence=evidence)
    return ProviderFault(source, "unknown", evidence=evidence)


_MINIMAX_STATUS_KINDS: dict[int, tuple[ProviderFaultKind, bool]] = {
    1001: ("timeout", True),
    1002: ("rate_limited", True),
    1004: ("unauthorized", False),
    1008: ("payment_required", False),
    1024: ("unavailable", True),
    1039: ("rate_limited", True),
    2013: ("bad_request", False),
    2049: ("unauthorized", False),
}


def classify_minimax_status(
    source: Subsystem, code: int, detail: str | None = None
) -> ProviderFault:
    kind, retryable = _MINIMAX_STATUS_KINDS.get(code, ("unknown", False))
    return ProviderFault(source, kind, evidence=FaultEvidence(code, detail), retryable=retryable)


def category_fault(source: Subsystem, error: object) -> ProviderFault | None:
    category = getattr(error, "category", None)
    name = getattr(category, "name", None)
    if not isinstance(name, str):
        return None
    mapped = ERROR_CATEGORY_KINDS.get(name.upper())
    if mapped is None:
        return None
    kind, retryable = mapped
    status = getattr(error, "status", None)
    detail = getattr(error, "detail", None)
    return ProviderFault(
        source,
        kind,
        evidence=FaultEvidence(
            status=status if isinstance(status, int) else None,
            detail=detail if isinstance(detail, str) else None,
        ),
        retryable=retryable,
    )


def classify_exception(
    source: Subsystem, error: BaseException, *, operation: str | None = None
) -> ProviderFault:
    if isinstance(error, asyncio.CancelledError):
        raise error
    if isinstance(error, ProviderFault):
        return error
    fault = _classify(source, error)
    fault.operation = operation
    return fault


def _classify(source: Subsystem, error: BaseException) -> ProviderFault:
    categorized = category_fault(source, error)
    if categorized is not None:
        return categorized
    if isinstance(error, FakeYouError):
        return ProviderFault(source, "unknown", evidence=FaultEvidence(detail=str(error)))
    if isinstance(error, (APITimeoutError, httpx2.TimeoutException, TimeoutError)):
        return ProviderFault(source, "timeout", retryable=True)
    if isinstance(error, APIStatusError):
        return classify_http(source, error.status_code)
    if isinstance(error, httpx2.HTTPStatusError):
        return classify_http(source, error.response.status_code)
    if isinstance(error, (APIConnectionError, httpx2.ConnectError, httpx2.NetworkError)):
        return ProviderFault(source, "unavailable", retryable=True)
    return ProviderFault(source, "unknown", evidence=FaultEvidence(detail=type(error).__name__))
