from fakeyou.perception.budget import (
    PROMPT_TOKEN_ALLOWANCE,
    PerceptionBudget,
    estimate_image_tokens,
    tokens_used,
    usd_for,
)
from tests._persistence_fakes import RecordingJournal


def test_image_estimates_count_tiles_plus_the_prompt() -> None:
    assert estimate_image_tokens(28, 28) == 1 + PROMPT_TOKEN_ALLOWANCE
    assert estimate_image_tokens(1568, 882) == 56 * 32 + PROMPT_TOKEN_ALLOWANCE


def test_reported_usage_wins_over_the_estimate() -> None:
    assert tokens_used({"total_tokens": 1234}, 99) == 1234
    assert tokens_used({"total_tokens": 0}, 99) == 99
    assert tokens_used({"total_tokens": True}, 99) == 99
    assert tokens_used(None, 99) == 99


def test_a_charged_reservation_moves_the_estimate_into_spend() -> None:
    budget = PerceptionBudget(budget_usd=1.0, max_calls=10)

    reserved = budget.reserve(2000, operation="read_screen")
    assert reserved is not None
    with reserved as reservation:
        cost = reservation.charge(1500)

    assert cost == usd_for(1500)
    assert budget.spent_usd == usd_for(1500)
    assert budget.calls == 1


def test_an_uncharged_reservation_is_refunded() -> None:
    budget = PerceptionBudget(budget_usd=1.0, max_calls=1)
    reservation = budget.reserve(2000, operation="read_screen")
    assert reservation is not None

    with reservation:
        pass

    assert budget.calls == 0
    assert budget.reserve(2000, operation="read_screen") is not None


def test_concurrent_reservations_cannot_overspend() -> None:
    budget = PerceptionBudget(budget_usd=usd_for(3000), max_calls=10)

    first = budget.reserve(2000, operation="read_screen")
    second = budget.reserve(2000, operation="read_screen")

    assert first is not None
    assert second is None


def test_exhaustion_is_journaled_once() -> None:
    journal = RecordingJournal()
    budget = PerceptionBudget(budget_usd=1.0, max_calls=1, journal=journal)
    reservation = budget.reserve(10, operation="read_screen")
    assert reservation is not None
    reservation.charge(10)

    assert budget.exhausted
    assert budget.reserve(10, operation="look") is None
    assert budget.reserve(10, operation="look") is None
    [event] = journal.events("budget_exhausted")
    assert (event["source"], event["operation"], event["calls"]) == ("perception", "look", 1)
