from dataclasses import dataclass
from typing import Any, cast

from fakeyou.journal import ProviderJournal
from fakeyou.provider_faults import ProviderFault, trim_detail

_MAX_FAULT_RECORDS = 128


@dataclass(frozen=True, slots=True)
class FaultSummary:
    source: str
    kind: str
    count: int
    status: int | None
    detail: str | None
    first_at: str


@dataclass(slots=True)
class _FaultRecord:
    source: str
    kind: str
    count: int
    status: int | None
    detail: str | None
    first_at: str


def is_recordable_fault(fault: object) -> bool:
    return (
        isinstance(fault, ProviderFault)
        or isinstance(getattr(fault, "source", None), str)
        and isinstance(getattr(fault, "kind", None), str)
    )


class FaultReporter:
    def __init__(self, *, journal: ProviderJournal) -> None:
        self.journal = journal
        self._records: dict[tuple[str, str, int | None], _FaultRecord] = {}
        self._total = 0

    def record(self, fault: object) -> None:
        try:
            if not is_recordable_fault(fault):
                return
            typed_fault = cast(Any, fault)
            source = trim_detail(
                typed_fault.source if isinstance(typed_fault.source, str) else None
            )
            kind = trim_detail(typed_fault.kind if isinstance(typed_fault.kind, str) else None)
            if not source or not kind:
                return
            source = source[:64]
            kind = kind[:64]
            raw_status = getattr(typed_fault, "status", None)
            status = (
                raw_status
                if isinstance(raw_status, int) and not isinstance(raw_status, bool)
                else None
            )
            key = (source, kind, status)
            self._total += 1
            record = self._records.get(key)
            if record is not None:
                record.count += 1
                return
            if len(self._records) >= _MAX_FAULT_RECORDS:
                return
            raw_detail = getattr(typed_fault, "detail", None)
            record = _FaultRecord(
                source=source,
                kind=kind,
                count=1,
                status=status,
                detail=trim_detail(raw_detail if isinstance(raw_detail, str) else None),
                first_at="",
            )
            self._records[key] = record
            record.first_at = (
                self.journal.record(
                    "fault", source=source, kind=kind, status=status, detail=record.detail
                )
                or ""
            )
        except BaseException:
            return

    def summaries(self) -> tuple[FaultSummary, ...]:
        rows = sorted(
            self._records.values(),
            key=lambda row: (-row.count, row.source, row.kind, row.status or -1),
        )
        return tuple(
            FaultSummary(r.source, r.kind, r.count, r.status, r.detail, r.first_at) for r in rows
        )

    def report(self) -> dict[str, object] | None:
        try:
            entries = self.summaries()
            if not entries:
                return None
            return {
                "faults": {
                    "kinds": [
                        {"kind": e.kind, "count": e.count, "status": e.status, "detail": e.detail}
                        for e in entries
                    ],
                    "total": self._total,
                }
            }
        except BaseException:
            return None

    def total(self) -> int:
        return self._total
