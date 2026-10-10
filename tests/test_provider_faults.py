import asyncio

import httpx
import httpx2
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pipecat.utils.errors import ErrorCategory

from fakeyou.observability.provider_faults import (
    ERROR_CATEGORY_KINDS,
    FaultEvidence,
    ProviderFault,
    classify_exception,
    classify_http,
)


def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.openai.com/v1/audio/speech")


def _status_error(status: int) -> APIStatusError:
    request = _request()
    response = httpx2.Response(status, request=request)
    return APIStatusError(f"provider said {status}", response=response, body=None)


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (402, "payment_required", False),
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (429, "rate_limited", True),
        (500, "unavailable", True),
        (503, "unavailable", True),
        (400, "bad_request", False),
        (404, "bad_request", False),
        (418, "bad_request", False),
        (302, "unknown", False),
    ],
)
def test_classify_http_status_matrix(status: int, kind: str, retryable: bool) -> None:
    fault = classify_http("tts", status, "backend words")

    assert fault.source == "tts"
    assert fault.kind == kind
    assert fault.status == status
    assert fault.retryable is retryable
    assert "tts" in str(fault)
    assert kind in str(fault)


def test_classify_http_truncates_backend_detail() -> None:
    fault = classify_http("stt", 500, "x" * 500)

    assert fault.detail is not None
    assert len(fault.detail) <= 300


@pytest.mark.parametrize(
    ("category", "kind", "retryable"),
    [
        (ErrorCategory.AUTHENTICATION, "unauthorized", False),
        (ErrorCategory.AUTHORIZATION, "unauthorized", False),
        (ErrorCategory.QUOTA, "payment_required", False),
        (ErrorCategory.RATE_LIMIT, "rate_limited", True),
        (ErrorCategory.CONNECTIVITY, "unavailable", True),
        (ErrorCategory.SERVER, "unavailable", True),
        (ErrorCategory.INVALID_REQUEST, "bad_request", False),
        (ErrorCategory.APPLICATION, "unknown", False),
        (ErrorCategory.UNKNOWN, "unknown", False),
    ],
)
def test_error_category_map_covers_all_nine_values(
    category: ErrorCategory, kind: str, retryable: bool
) -> None:
    assert ERROR_CATEGORY_KINDS[category.name] == (kind, retryable)


def test_sdk_timeout_maps_to_retryable_timeout() -> None:
    fault = classify_exception("tts", APITimeoutError(_request()))

    assert fault.kind == "timeout"
    assert fault.retryable is True
    assert fault.source == "tts"


def test_sdk_status_error_maps_through_http_table() -> None:
    fault = classify_exception("tts", _status_error(401))

    assert fault.kind == "unauthorized"
    assert fault.status == 401
    assert fault.retryable is False


def test_sdk_connection_error_maps_to_unavailable() -> None:
    error = APIConnectionError(message="conn reset", request=_request())

    fault = classify_exception("tts", error)

    assert fault.kind == "unavailable"
    assert fault.retryable is True


def test_httpx2_timeout_maps_to_retryable_timeout() -> None:
    fault = classify_exception("tts", httpx2.TimeoutException("read stalled"))

    assert fault.kind == "timeout"
    assert fault.retryable is True


def test_httpx2_status_error_maps_through_http_table() -> None:
    request = _request()
    response = httpx2.Response(429, request=request)
    error = httpx2.HTTPStatusError("too many", request=request, response=response)

    fault = classify_exception("tts", error)

    assert fault.kind == "rate_limited"
    assert fault.status == 429
    assert fault.retryable is True


@pytest.mark.parametrize("error", [httpx2.ConnectError("boom"), httpx2.NetworkError("gone")])
def test_httpx2_network_errors_map_to_unavailable(error: httpx2.NetworkError) -> None:
    fault = classify_exception("stt", error)

    assert fault.kind == "unavailable"
    assert fault.retryable is True


def test_legacy_httpx_error_is_not_specially_classified() -> None:
    fault = classify_exception("tts", httpx.ConnectError("legacy boom"))

    assert fault.kind == "unknown"
    assert fault.retryable is False


def test_plain_timeout_error_maps_once() -> None:
    assert asyncio.TimeoutError is TimeoutError

    fault = classify_exception("agent", TimeoutError("slow"))

    assert fault.kind == "timeout"
    assert fault.retryable is True


def test_unknown_error_carries_class_name_only() -> None:
    fault = classify_exception("agent", ValueError("weird shape"))

    assert fault.kind == "unknown"
    assert fault.retryable is False
    assert fault.detail == "ValueError"


def test_cancelled_error_is_never_classified() -> None:
    with pytest.raises(asyncio.CancelledError):
        classify_exception("tts", asyncio.CancelledError())


def test_provider_fault_passes_through_unchanged() -> None:
    original = ProviderFault(
        "stt", "unavailable", evidence=FaultEvidence(detail="down"), retryable=True
    )

    assert classify_exception("stt", original) is original


def test_provider_fault_renders_source_and_kind() -> None:
    fault = ProviderFault("agent", "timeout", evidence=FaultEvidence(None, "slow"), retryable=True)

    assert str(fault).startswith("agent timeout")
    assert isinstance(fault, Exception)


def test_provider_fault_truncates_long_detail() -> None:
    fault = ProviderFault("tts", "unknown", evidence=FaultEvidence(detail="y" * 900))

    assert fault.detail is not None
    assert len(fault.detail) <= 300


def test_sdk_error_detail_does_not_include_provider_payload_or_credentials() -> None:
    request = httpx2.Request(
        "POST",
        "https://api.openai.com/v1/audio/speech?api_key=sk-secret",
        headers={"authorization": "Bearer top-secret"},
    )
    response = httpx2.Response(500, request=request, content=b'{"error":"raw provider response"}')
    fault = classify_exception(
        "tts", APIStatusError("raw provider response", response=response, body={"secret": "x"})
    )

    assert fault.detail is None or "raw provider response" not in fault.detail
    assert fault.detail is None or "sk-secret" not in fault.detail
    assert fault.detail is None or "top-secret" not in fault.detail
