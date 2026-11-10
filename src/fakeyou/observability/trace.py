from __future__ import annotations

import secrets
from collections.abc import Mapping

from opentelemetry import propagate, trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

TRACEPARENT_HEADER = "traceparent"
TRACEPARENT_ENV = "TRACEPARENT"
_ZERO_TRACE_ID = "0" * 32


def mint_trace_id() -> str:
    return secrets.token_hex(16)


def is_trace_id(trace_id: str) -> bool:
    return len(trace_id) == 32 and trace_id != _ZERO_TRACE_ID and _is_hex(trace_id)


def _is_hex(value: str) -> bool:
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def traceparent_for(trace_id: str) -> str:
    context = trace.set_span_in_context(
        NonRecordingSpan(
            SpanContext(
                trace_id=int(trace_id, 16),
                span_id=int(secrets.token_hex(8), 16),
                is_remote=True,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
        )
    )
    carrier: dict[str, str] = {}
    propagate.inject(carrier, context=context)
    return carrier[TRACEPARENT_HEADER]


def trace_id_from(carrier: Mapping[str, str]) -> str | None:
    lowered = {key.lower(): value for key, value in carrier.items()}
    context = propagate.extract(lowered)
    span_context = trace.get_current_span(context).get_span_context()
    if not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x")
