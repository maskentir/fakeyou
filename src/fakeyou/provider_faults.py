import asyncio
import re
from typing import Literal

import httpx2
from openai import APIConnectionError, APIStatusError, APITimeoutError

ProviderFaultKind = Literal[
    "payment_required",
    "unauthorized",
    "rate_limited",
    "unavailable",
    "timeout",
    "bad_request",
    "unknown",
]

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
MAX_SOURCE_CHARS = 64
_FAULT_KINDS = frozenset(
    {
        "payment_required",
        "unauthorized",
        "rate_limited",
        "unavailable",
        "timeout",
        "bad_request",
        "unknown",
    }
)
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


def _safe_label(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    value = trim_detail(value) or fallback
    value = re.sub(r"[^A-Za-z0-9_.:-]+", "_", value)
    return value[:MAX_SOURCE_CHARS] or fallback


class ProviderFault(Exception):
    def __init__(
        self,
        source: str,
        kind: ProviderFaultKind,
        *,
        status: int | None = None,
        detail: str | None = None,
        retryable: bool = False,
    ) -> None:
        self.source = _safe_label(source, "provider")
        self.kind = kind if kind in _FAULT_KINDS else "unknown"
        self.status = status if isinstance(status, int) and not isinstance(status, bool) else None
        self.detail = trim_detail(detail)
        self.retryable = bool(retryable)
        message = f"{self.source} {self.kind}"
        if self.status is not None:
            message = f"{message} (status {self.status})"
        if self.detail:
            message = f"{message}: {self.detail}"
        super().__init__(message)


def classify_http(source: str, status: int, detail: str | None = None) -> ProviderFault:
    if status == 402:
        return ProviderFault(source, "payment_required", status=status, detail=detail)
    if status in (401, 403):
        return ProviderFault(source, "unauthorized", status=status, detail=detail)
    if status == 429:
        return ProviderFault(source, "rate_limited", status=status, detail=detail, retryable=True)
    if 500 <= status <= 599:
        return ProviderFault(source, "unavailable", status=status, detail=detail, retryable=True)
    if 400 <= status <= 499:
        return ProviderFault(source, "bad_request", status=status, detail=detail)
    return ProviderFault(source, "unknown", status=status, detail=detail)


def _category_fault(source: str, error: BaseException) -> ProviderFault | None:
    category = getattr(error, "category", None) or getattr(error, "error_category", None)
    name = getattr(category, "name", None) or getattr(category, "value", None)
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
        status=status if isinstance(status, int) else None,
        detail=detail if isinstance(detail, str) else None,
        retryable=retryable,
    )


def classify_exception(source: str, error: BaseException) -> ProviderFault:
    if isinstance(error, asyncio.CancelledError):
        raise error
    if isinstance(error, ProviderFault):
        return error
    category_fault = _category_fault(source, error)
    if category_fault is not None:
        return category_fault
    if isinstance(error, (APITimeoutError, httpx2.TimeoutException, TimeoutError)):
        return ProviderFault(source, "timeout", retryable=True)
    if isinstance(error, APIStatusError):
        return classify_http(source, error.status_code)
    if isinstance(error, httpx2.HTTPStatusError):
        return classify_http(source, error.response.status_code)
    if isinstance(error, (APIConnectionError, httpx2.ConnectError, httpx2.NetworkError)):
        return ProviderFault(source, "unavailable", retryable=True)
    if isinstance(error, ValueError):
        return ProviderFault(source, "unknown", detail=f"ValueError: {error}")
    return ProviderFault(source, "unknown", detail=type(error).__name__)
