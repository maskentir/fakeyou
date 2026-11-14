from __future__ import annotations

import fcntl
import json
import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class LedgerError(RuntimeError):
    pass


class BudgetLimits:
    def __init__(
        self,
        experiment_id: str,
        max_attempts: int,
        max_usd: Decimal,
        max_concurrent: int,
        roles: frozenset[str],
    ) -> None:
        if not experiment_id or max_attempts < 0 or max_concurrent < 0 or max_usd < 0:
            raise ValueError("invalid budget limits")
        if not roles or any(not role for role in roles):
            raise ValueError("roles must be non-empty")
        self.experiment_id = experiment_id
        self.max_attempts = max_attempts
        self.max_usd = Decimal(max_usd)
        self.max_concurrent = max_concurrent
        self.roles = frozenset(roles)

    def as_json(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "max_attempts": self.max_attempts,
            "max_usd": str(self.max_usd),
            "max_concurrent": self.max_concurrent,
            "roles": sorted(self.roles),
        }


class MeasurementLedger:
    def __init__(
        self,
        path: Path,
        limits: BudgetLimits,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.limits = limits
        self.clock = clock or (lambda: datetime.now(UTC).isoformat())
        self._initialize()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _initialize(self) -> None:
        with self._locked():
            if not self.path.exists():
                self._write({"version": 1, "limits": self.limits.as_json(), "attempts": []})
                return
            data = self._read()
            if data.get("version") != 1 or data.get("limits") != self.limits.as_json():
                raise LedgerError("incompatible existing ledger")

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise LedgerError("incompatible existing ledger") from error
        if not isinstance(value, dict) or not isinstance(value.get("attempts"), list):
            raise LedgerError("incompatible existing ledger")
        return value

    def _write(self, data: dict[str, Any]) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    @staticmethod
    def _decimal(value: Decimal | str | int | float) -> Decimal:
        try:
            result = Decimal(value)
        except (InvalidOperation, ValueError) as error:
            raise LedgerError("invalid USD amount") from error
        if result < 0 or not result.is_finite():
            raise LedgerError("invalid USD amount")
        return result

    def reserve(
        self,
        role: str,
        provider: str,
        model: str,
        estimate_usd: Decimal,
        *,
        turn_id: str | None = None,
        parent_attempt_id: str | None = None,
    ) -> str:
        estimate = self._decimal(estimate_usd)
        with self._locked():
            data = self._read()
            attempts = data["attempts"]
            if role not in self.limits.roles:
                raise LedgerError("undeclared role")
            if len(attempts) >= self.limits.max_attempts:
                raise LedgerError("request cap exhausted")
            if self._breach(attempts):
                raise LedgerError("accounting breach")
            active = sum(entry.get("status") in {"reserved", "sent"} for entry in attempts)
            if active >= self.limits.max_concurrent:
                raise LedgerError("concurrency cap exhausted")
            known = sum(
                (
                    self._decimal(entry.get("usd", "0"))
                    for entry in attempts
                    if entry.get("usd") is not None
                ),
                Decimal("0.00"),
            )
            held = sum(
                (
                    self._decimal(entry.get("estimate_usd", "0"))
                    for entry in attempts
                    if entry.get("status") in {"reserved", "sent", "unknown"}
                ),
                Decimal("0.00"),
            )
            if known + held + estimate > self.limits.max_usd:
                raise LedgerError("USD cap exhausted")
            attempt_id = str(uuid.uuid4())
            attempts.append(
                {
                    "id": attempt_id,
                    "role": role,
                    "provider": provider,
                    "model": model,
                    "estimate_usd": str(estimate),
                    "usd": None,
                    "status": "reserved",
                    "sent": False,
                    "inference": None,
                    "outcome": None,
                    "turn_id": turn_id,
                    "parent_attempt_id": parent_attempt_id,
                    "at": self.clock(),
                }
            )
            self._write(data)
            return attempt_id

    def mark_sent(self, attempt_id: str) -> None:
        with self._locked():
            data = self._read()
            entry = self._entry(data, attempt_id)
            if entry["status"] != "reserved":
                raise LedgerError("attempt is not reservable")
            entry["status"] = "sent"
            entry["sent"] = True
            entry["sent_at"] = self.clock()
            self._write(data)

    def settle(
        self,
        attempt_id: str,
        *,
        usd: Decimal | None,
        inference: bool | None,
        outcome: str,
    ) -> None:
        amount = None if usd is None else self._decimal(usd)
        with self._locked():
            data = self._read()
            entry = self._entry(data, attempt_id)
            if entry["status"] not in {"reserved", "sent"}:
                raise LedgerError("attempt is already settled")
            entry["status"] = "unknown" if amount is None else "settled"
            entry["usd"] = None if amount is None else str(amount)
            entry["overrun"] = amount is not None and amount > self._decimal(entry["estimate_usd"])
            entry["inference"] = inference
            entry["outcome"] = outcome
            entry["settled_at"] = self.clock()
            self._write(data)

    @staticmethod
    def _entry(data: dict[str, Any], attempt_id: str) -> dict[str, Any]:
        for entry in data["attempts"]:
            if entry.get("id") == attempt_id:
                found: dict[str, Any] = entry
                return found
        raise LedgerError("unknown attempt")

    def _breach(self, attempts: list[dict[str, Any]]) -> bool:
        known = sum(
            self._decimal(entry.get("usd", "0"))
            for entry in attempts
            if entry.get("usd") is not None
        )
        return known > self.limits.max_usd or any(
            entry.get("overrun") is True for entry in attempts
        )

    def snapshot(self) -> dict[str, Any]:
        with self._locked():
            data = self._read()
            attempts = [dict(entry) for entry in data["attempts"]]
            known = sum(
                (self._decimal(entry["usd"]) for entry in attempts if entry.get("usd") is not None),
                Decimal("0.00"),
            )
            held = sum(
                (
                    self._decimal(entry["estimate_usd"])
                    for entry in attempts
                    if entry.get("status") in {"reserved", "sent", "unknown"}
                ),
                Decimal("0.00"),
            )
            breach = self._breach(attempts) or known + held > self.limits.max_usd
            return {
                "experiment_id": self.limits.experiment_id,
                "limits": self.limits.as_json(),
                "attempts": attempts,
                "attempt_count": len(attempts),
                "active_count": sum(
                    entry.get("status") in {"reserved", "sent"} for entry in attempts
                ),
                "known_usd": str(known),
                "held_usd": str(held),
                "unknown_usd": str(
                    sum(
                        self._decimal(entry["estimate_usd"])
                        for entry in attempts
                        if entry.get("status") == "unknown"
                    )
                ),
                "upper_bound_usd": str(known + held),
                "breach": breach,
            }
