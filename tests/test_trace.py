from fakeyou.observability.trace import (
    is_trace_id,
    mint_trace_id,
    trace_id_from,
    traceparent_for,
)

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


def test_a_minted_trace_id_is_a_valid_w3c_trace_id() -> None:
    assert is_trace_id(mint_trace_id())
    assert mint_trace_id() != mint_trace_id()
    assert not is_trace_id("0" * 32)
    assert not is_trace_id("not-hex")


def test_trace_id_roundtrips_through_a_traceparent_header() -> None:
    header = traceparent_for(TRACE_ID)

    assert header.startswith(f"00-{TRACE_ID}-")
    assert trace_id_from({"traceparent": header}) == TRACE_ID
    assert trace_id_from({"Traceparent": TRACEPARENT}) == TRACE_ID


def test_missing_or_invalid_traceparents_yield_no_trace_id() -> None:
    assert trace_id_from({}) is None
    assert trace_id_from({"traceparent": "garbage"}) is None
    assert trace_id_from({"traceparent": f"00-{'0' * 32}-00f067aa0ba902b7-01"}) is None
