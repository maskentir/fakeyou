import json
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConsumerFault:
    source: str = "stt"
    kind: str = "callback"
    status: int | None = None
    detail: str | None = None


def _default_log(line: str) -> None:
    print(line, flush=True)


def record_consumer_fault(
    reporter: object, log: Callable[[str], None] | None, detail: str, error: BaseException
) -> None:
    fault = ConsumerFault(detail=f"{detail}: {type(error).__name__}")
    record = getattr(reporter, "record", None)
    if record is not None:
        try:
            record(fault)
        except Exception:
            pass
    try:
        (log or _default_log)(
            json.dumps({"event": "callback_fault", "detail": detail, "error": type(error).__name__})
        )
    except Exception:
        pass
