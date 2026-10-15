import asyncio
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

from fakeyou.foundation.polling import PollGuard
from fakeyou.foundation.taxonomy import FaultKind, Subsystem
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import ProviderFault, classify_exception, trim_detail

_MAX_FAULT_RECORDS = 128

BUG_ERRORS = (AssertionError, AttributeError, IndexError, KeyError, TypeError, ValueError)


@dataclass(frozen=True, slots=True)
class Fault:
    source: Subsystem
    kind: FaultKind
    status: int | None = None
    detail: str | None = None
    operation: str | None = None
    count: int = 1
    first_at: str = ""


@dataclass(frozen=True, slots=True)
class ConsumerSink:
    faults: "FaultReporter | None" = None
    journal: Journal | None = None


def record_consumer_fault(
    sink: ConsumerSink,
    *,
    source: Subsystem,
    detail: str,
    error: BaseException,
    operation: str | None = None,
) -> None:
    fault = Fault(
        source=source,
        kind="callback_failed",
        detail=f"{detail}: {type(error).__name__}",
        operation=operation,
    )
    if sink.faults is not None:
        sink.faults.record(fault)
    elif sink.journal is not None:
        sink.journal.record(
            "fault_recorded",
            source=fault.source,
            kind=fault.kind,
            detail=fault.detail,
            operation=fault.operation,
        )


async def guard_consumer(
    call: Callable[[], Awaitable[None]],
    *,
    sink: ConsumerSink,
    source: Subsystem,
    detail: str,
    timeout_seconds: float = 10.0,
) -> None:
    try:
        async with asyncio.timeout(timeout_seconds):
            await call()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        record_consumer_fault(sink, source=source, detail=detail, error=error)


def guard_observer(
    call: Callable[[], None],
    *,
    sink: ConsumerSink,
    source: Subsystem,
    detail: str,
    operation: str | None = None,
) -> None:
    try:
        call()
    except Exception as error:
        record_consumer_fault(sink, source=source, detail=detail, error=error, operation=operation)


async def guard_poll(
    call: Callable[[], Awaitable[None]],
    *,
    sink: ConsumerSink,
    source: Subsystem,
    operation: str,
) -> Exception | None:
    try:
        await call()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        if sink.faults is not None:
            sink.faults.record(classify_exception(source, error, operation=operation))
        else:
            record_consumer_fault(
                sink, source=source, detail=operation, error=error, operation=operation
            )
        return error
    return None


def create_poll_guard(
    sink: ConsumerSink | None, *, source: Subsystem, operation: str
) -> PollGuard | None:
    if sink is None:
        return None
    return partial(guard_poll, sink=sink, source=source, operation=operation)


async def capture_error[T](
    call: Callable[[], Awaitable[T]],
) -> tuple[T | None, Exception | None]:
    try:
        return await call(), None
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return None, error


TRACEBACK_MAX_FRAMES = 5
TRACEBACK_MAX_CHARS = 2000


def bounded_traceback(error: BaseException) -> str:
    footer = "".join(traceback.format_exception_only(error)).strip()
    tail = traceback.extract_tb(error.__traceback__)[-TRACEBACK_MAX_FRAMES:]
    lines = [footer]
    for frame in reversed(tail):
        lines.append(f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}')
    return "\n".join(lines)[:TRACEBACK_MAX_CHARS]


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
        except Exception as error:
            self._report_failure("record", error)

    def _report_failure(self, operation: str, error: Exception) -> None:
        self.journal.record(
            "fault_report_failed",
            source="runtime",
            operation=operation,
            error_type=type(error).__name__,
        )

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

    def report(self) -> dict[str, Any] | None:
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
        except Exception as error:
            self._report_failure("report", error)
            return None

    def total(self) -> int:
        return self._total
