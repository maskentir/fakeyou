import asyncio
import json
from pathlib import Path

import pytest

from evals._lineage_classifier import Pricing
from evals._recording_budget import (
    RecordingBudget,
    RecordingCaps,
    RecordingLedger,
    RecordingStopped,
    usage_tokens,
)

ROUTE = "/v1beta/models/gemini-3.1-flash-lite:generateContent"
STRONG = "/v1beta/models/gemini-3.8-flash:generateContent"
BODY = b'{"contents": []}'
WORST = (len(BODY) * 0.25 + 65536 * 1.5) / 1_000_000
USAGE = {"promptTokenCount": 1000, "candidatesTokenCount": 20, "thoughtsTokenCount": 30}
ANSWER = json.dumps({"candidates": [], "usageMetadata": USAGE})


def _budget(tmp_path: Path, *, attempts: int = 100, spend: float = 0.25) -> RecordingBudget:
    path = tmp_path / "budget.json"
    ledger = RecordingLedger(
        caps=RecordingCaps(
            max_attempts=attempts, max_spend_usd=spend, max_output_tokens_per_call=65536
        ),
        pricing={
            "gemini-3.1-flash-lite": Pricing(
                input_per_million_usd=0.25,
                output_per_million_usd=1.5,
                source="test",
                retrieved="2026-11-21",
                valid_until="2026-12-31",
            )
        },
    )
    path.write_text(ledger.model_dump_json(), encoding="utf-8")
    return RecordingBudget.load(path)


async def test_a_call_is_charged_its_measured_usage_and_releases_its_reservation(
    tmp_path: Path,
) -> None:
    budget = _budget(tmp_path)

    reservation = await budget.admit(ROUTE, BODY)
    await budget.settle(ROUTE, reservation, 200, ANSWER)
    report = budget.ledger.report()

    assert reservation == pytest.approx(WORST)
    assert budget.in_flight == []
    assert report["input_tokens"] == 1000
    assert report["output_tokens"] == 50
    assert report["estimated_spend_usd"] == pytest.approx((1000 * 0.25 + 50 * 1.5) / 1e6)
    assert report["stop_reason"] is None


async def test_the_worst_case_is_reserved_before_forwarding(tmp_path: Path) -> None:
    budget = _budget(tmp_path, spend=WORST / 2)

    with pytest.raises(RecordingStopped, match="spend_limit"):
        await budget.admit(ROUTE, BODY)

    assert budget.ledger.report()["attempts"] == 0


async def test_the_attempt_limit_counts_calls_in_flight(tmp_path: Path) -> None:
    budget = _budget(tmp_path, attempts=2)

    await budget.admit(ROUTE, BODY)
    await budget.admit(ROUTE, BODY)

    with pytest.raises(RecordingStopped, match="attempt_limit"):
        async with asyncio.timeout(1):
            await budget.admit(ROUTE, BODY)


async def test_a_call_waits_for_reservations_in_flight_instead_of_overspending(
    tmp_path: Path,
) -> None:
    budget = _budget(tmp_path, spend=WORST * 1.5)
    first = await budget.admit(ROUTE, BODY)

    second = asyncio.ensure_future(budget.admit(ROUTE, BODY))
    await asyncio.sleep(0.05)
    waited = not second.done()
    await budget.settle(ROUTE, first, 200, ANSWER)

    assert waited
    assert await second == pytest.approx(WORST)


@pytest.mark.parametrize(
    ("status", "answer", "reason"),
    [
        (429, '{"error": {"code": 429}}', "provider_failure"),
        (0, "", "provider_failure"),
        (200, json.dumps({"candidates": []}), "usage_unknown"),
    ],
)
async def test_a_failed_or_unmetered_call_is_charged_in_full_and_stops_the_batch(
    tmp_path: Path, status: int, answer: str, reason: str
) -> None:
    budget = _budget(tmp_path)

    reservation = await budget.admit(ROUTE, BODY)
    await budget.settle(ROUTE, reservation, status, answer)

    assert budget.ledger.report()["estimated_spend_usd"] == pytest.approx(WORST, abs=1e-6)
    assert budget.ledger.stop_reason == reason
    with pytest.raises(RecordingStopped, match=reason):
        await budget.admit(ROUTE, BODY)


async def test_a_model_without_a_verified_price_is_never_forwarded(tmp_path: Path) -> None:
    budget = _budget(tmp_path)

    with pytest.raises(RecordingStopped, match="unknown_pricing"):
        await budget.admit(STRONG, BODY)


async def test_spend_attempts_and_a_stop_carry_over_to_the_next_scenario_run(
    tmp_path: Path,
) -> None:
    first_run = _budget(tmp_path, attempts=3)
    reservation = await first_run.admit(ROUTE, BODY)
    await first_run.settle(ROUTE, reservation, 200, ANSWER)
    stopped = await first_run.admit(ROUTE, BODY)
    await first_run.settle(ROUTE, stopped, 503, "")

    next_run = RecordingBudget.load(first_run.path)

    assert next_run.ledger.report()["attempts"] == 2
    with pytest.raises(RecordingStopped, match="provider_failure"):
        await next_run.admit(ROUTE, BODY)


def test_usage_is_read_from_the_last_event_of_a_streamed_answer() -> None:
    first = {"candidates": [], "usageMetadata": {"promptTokenCount": 10}}
    last = {"candidates": [], "usageMetadata": USAGE}
    streamed = f"data: {json.dumps(first)}\r\n\r\ndata: {json.dumps(last)}\r\n\r\n"

    assert usage_tokens(streamed) == (1000, 50)
    assert usage_tokens("not json") is None
