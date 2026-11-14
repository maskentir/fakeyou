from decimal import Decimal
from pathlib import Path
from threading import Barrier, Thread

import pytest

from evals._measurement_budget import BudgetLimits, LedgerError, MeasurementLedger

ROLES = frozenset(
    {
        "vision",
        "background_vision",
        "judge",
        "reply",
        "stt",
        "tts",
        "embedding",
        "memory",
        "debrief",
    }
)


def limits(
    *,
    experiment_id: str = "offline-1",
    max_attempts: int = 20,
    max_usd: Decimal = Decimal("1.00"),
    max_concurrent: int = 2,
    roles: frozenset[str] = ROLES,
) -> BudgetLimits:
    return BudgetLimits(experiment_id, max_attempts, max_usd, max_concurrent, roles)


def test_reservation_and_settlement_preserve_attempt_and_billing_bounds(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = MeasurementLedger(path, limits())
    attempt = ledger.reserve("vision", "fake", "model", Decimal("0.20"), turn_id="t1")
    ledger.mark_sent(attempt)
    ledger.settle(attempt, usd=Decimal("0.12"), inference=True, outcome="ok")
    result = ledger.snapshot()
    assert result["known_usd"] == "0.12"
    assert result["held_usd"] == "0.00"
    assert result["attempts"][0]["id"] == attempt
    assert result["attempts"][0]["sent"] is True
    assert result["attempts"][0]["inference"] is True


@pytest.mark.parametrize("role", sorted(ROLES))
def test_all_declared_paid_roles_are_reservable(tmp_path: Path, role: str) -> None:
    ledger = MeasurementLedger(tmp_path / f"{role}.json", limits(max_concurrent=20))
    assert ledger.reserve(role, "fake", "model", Decimal("0.01"))


def test_undeclared_role_and_zero_cap_are_refused(tmp_path: Path) -> None:
    ledger = MeasurementLedger(tmp_path / "ledger.json", limits(max_attempts=0))
    with pytest.raises(LedgerError, match="cap"):
        ledger.reserve("vision", "fake", "model", Decimal("0.01"))
    other = MeasurementLedger(tmp_path / "other.json", limits())
    with pytest.raises(LedgerError, match="role"):
        other.reserve("unknown", "fake", "model", Decimal("0.01"))


def test_unknown_charge_keeps_reserved_liability_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = MeasurementLedger(path, limits())
    attempt = ledger.reserve("tts", "fake", "model", Decimal("0.30"))
    ledger.mark_sent(attempt)
    ledger.settle(attempt, usd=None, inference=None, outcome="timeout")
    rebuilt = MeasurementLedger(path, limits())
    assert rebuilt.snapshot()["held_usd"] == "0.30"
    assert rebuilt.snapshot()["unknown_usd"] == "0.30"
    with pytest.raises(LedgerError):
        rebuilt.reserve("tts", "fake", "model", Decimal("0.71"))


def test_retry_and_fallback_retain_parent_attempt_linkage(tmp_path: Path) -> None:
    ledger = MeasurementLedger(tmp_path / "ledger.json", limits(max_concurrent=3))
    parent = ledger.reserve("vision", "fake", "a", Decimal("0.10"), turn_id="t")
    retry = ledger.reserve("vision", "fake", "b", Decimal("0.10"), parent_attempt_id=parent)
    snapshot = ledger.snapshot()
    assert snapshot["attempts"][1]["parent_attempt_id"] == parent
    assert retry != parent


def test_overrun_is_preserved_and_blocks_new_work(tmp_path: Path) -> None:
    ledger = MeasurementLedger(tmp_path / "ledger.json", limits(max_concurrent=2))
    attempt = ledger.reserve("judge", "fake", "model", Decimal("0.10"))
    ledger.settle(attempt, usd=Decimal("0.20"), inference=False, outcome="ok")
    snapshot = ledger.snapshot()
    assert snapshot["known_usd"] == "0.20"
    assert snapshot["breach"] is True
    with pytest.raises(LedgerError, match="breach"):
        ledger.reserve("reply", "fake", "model", Decimal("0.01"))


def test_incompatible_existing_ledger_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    MeasurementLedger(path, limits()).reserve("vision", "fake", "model", Decimal("0.01"))
    with pytest.raises(LedgerError, match="incompatible"):
        MeasurementLedger(path, limits(experiment_id="other"))


def test_competing_reservations_do_not_overspend(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    barrier = Barrier(2)
    results: list[str] = []

    def reserve() -> None:
        ledger = MeasurementLedger(path, limits(max_attempts=1, max_concurrent=2))
        barrier.wait()
        try:
            results.append(ledger.reserve("vision", "fake", "model", Decimal("0.10")))
        except LedgerError:
            results.append("rejected")

    threads = [Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([result for result in results if result != "rejected"]) == 1
    assert len([result for result in results if result == "rejected"]) == 1
