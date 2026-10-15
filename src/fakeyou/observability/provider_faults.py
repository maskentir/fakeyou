import asyncio
import re
from dataclasses import dataclass
from typing import Any, get_args
from urllib.parse import urlsplit, urlunsplit

import httpx2
from openai import APIConnectionError, APIStatusError, APITimeoutError

from fakeyou.catalog.errors import FakeYouError
from fakeyou.foundation.taxonomy import ProviderFaultKind, Subsystem

MAX_DETAIL_CHARS = 300
_FAULT_KINDS = frozenset(get_args(ProviderFaultKind))
_SECRET = re.compile(
    r"(?i)(bearer\s+|api[_-]?key[=:]\s*|token[=:]\s*|x-goog-api-key\s*[:=]\s*)[^\s,;]+"
)
_BASIC_AUTH = re.compile(r"(Basic\s+)[A-Za-z0-9+/=]{8,}")
_BARE_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")
_URL = re.compile(r"(?i)[a-z][a-z0-9+.-]*://\S+")
_JSON_SECRET = re.compile(
    r'(?i)("(?:api[_-]?key|authorization|token|password|secret|body|x-goog-api-key)"\s*:\s*)[^,}]+'
)


def trim_detail(detail: str | None) -> str | None:
    if detail is None:
        return None
    safe = _URL.sub("<url>", detail)
    safe = _SECRET.sub(r"\1<redacted>", safe)
    safe = _BASIC_AUTH.sub(r"\1<redacted>", safe)
    safe = _BARE_KEY.sub("<redacted>", safe)
    safe = _JSON_SECRET.sub(r"\1<redacted>", safe)
    return safe[:MAX_DETAIL_CHARS] or None


def safe_endpoint(endpoint: Any) -> str:
    if not isinstance(endpoint, str):
        return "<invalid endpoint>"
    try:
        parsed = urlsplit(endpoint)
        if parsed.hostname is None:
            return endpoint
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except (ValueError, UnicodeError):
        return endpoint


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


_PROGRAMMING_ERRORS = (AssertionError, AttributeError, IndexError, KeyError, TypeError)


def classify_exception(
    source: Subsystem, error: BaseException, *, operation: str | None = None
) -> ProviderFault:
    if isinstance(error, asyncio.CancelledError):
        raise error
    if isinstance(error, ProviderFault):
        return error
    if isinstance(error, _PROGRAMMING_ERRORS):
        fault = ProviderFault(
            source, "unknown", evidence=FaultEvidence(detail=f"internal:{type(error).__name__}")
        )
        fault.operation = operation
        return fault
    fault = _classify(source, error)
    fault.operation = operation
    return fault


def _classify(source: Subsystem, error: BaseException) -> ProviderFault:
    if isinstance(error, FakeYouError):
        return ProviderFault(source, "unknown", evidence=FaultEvidence(detail=error.code.value))
    if isinstance(error, (APITimeoutError, httpx2.TimeoutException, TimeoutError)):
        return ProviderFault(source, "timeout", retryable=True)
    if isinstance(error, APIStatusError):
        return classify_http(source, error.status_code)
    if isinstance(error, httpx2.HTTPStatusError):
        return classify_http(source, error.response.status_code)
    if isinstance(error, (APIConnectionError, httpx2.ConnectError, httpx2.NetworkError)):
        return ProviderFault(source, "unavailable", retryable=True)
    return ProviderFault(source, "unknown", evidence=FaultEvidence(detail=type(error).__name__))
