from dataclasses import dataclass, replace
from typing import Literal

from fakeyou.journal import Journal
from fakeyou.provider_faults import ProviderFault, ProviderFaultKind, trim_detail
from fakeyou.taxonomy import Subsystem

HealthKind = Literal[
    "no_audio_frames",
    "no_transcripts",
    "silent_capture",
    "reconnect_unsupported",
    "callback_failed",
    "voiceprint_misbind",
    "voiceprint_model_mismatch",
]
FaultKind = ProviderFaultKind | HealthKind

_MAX_FAULT_RECORDS = 128


@dataclass(frozen=True, slots=True)
class Fault:
    source: Subsystem
    kind: FaultKind
    status: int | None = None
    detail: str | None = None
    operation: str | None = None
    count: int = 1
    first_at: str = ""


def record_consumer_fault(
    faults: "FaultReporter | None",
    journal: Journal | None,
    *,
    source: Subsystem,
    detail: str,
    error: BaseException,
) -> None:
    fault = Fault(source=source, kind="callback_failed", detail=f"{detail}: {type(error).__name__}")
    if faults is not None:
        faults.record(fault)
    elif journal is not None:
        journal.record("fault_recorded", source=fault.source, kind=fault.kind, detail=fault.detail)


class FaultReporter:
    def __init__(self, *, journal: Journal) -> None:
        self.journal = journal
        self._records: dict[tuple[Subsystem, str | None, FaultKind, int | None], Fault] = {}
        self._total = 0

    def record(self, fault: ProviderFault | Fault) -> None:
        try:
            key = (fault.source, fault.operation, fault.kind, fault.status)
            self._total += 1
            existing = self._records.get(key)
            if existing is not None:
                self._records[key] = replace(existing, count=existing.count + 1)
                return
            if len(self._records) >= _MAX_FAULT_RECORDS:
                return
            detail = trim_detail(fault.detail)
            first_at = self.journal.record(
                "fault_recorded",
                source=fault.source,
                operation=fault.operation,
                kind=fault.kind,
                status=fault.status,
                detail=detail,
            )
            self._records[key] = Fault(
                source=fault.source,
                kind=fault.kind,
                status=fault.status,
                detail=detail,
                operation=fault.operation,
                first_at=first_at or "",
            )
        except Exception:
            return

    def summaries(self) -> tuple[Fault, ...]:
        rows = sorted(
            self._records.values(),
            key=lambda row: (
                -row.count,
                row.source,
                row.operation or "",
                row.kind,
                row.status or -1,
            ),
        )
        return tuple(rows)

    def report(self) -> dict[str, object] | None:
        try:
            entries = self.summaries()
            if not entries:
                return None
            return {
                "faults": {
                    "kinds": [
                        {
                            "source": e.source,
                            "operation": e.operation,
                            "kind": e.kind,
                            "count": e.count,
                            "status": e.status,
                            "detail": e.detail,
                        }
                        for e in entries
                    ],
                    "total": self._total,
                }
            }
        except Exception:
            return None

    def total(self) -> int:
        return self._total
