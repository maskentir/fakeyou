import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from typing import Any

from fakeyou.provider_faults import trim_detail

JOURNAL_BACKUP_COUNT = 2

_EVENT_REGISTRY: set[str] = {
    "synth_started",
    "synth_finished",
    "synth_failed",
    "stt_reconnect",
}
_SENSITIVE = {
    "authorization",
    "api_key",
    "apikey",
    "token",
    "password",
    "secret",
    "body",
    "transcript",
    "transcript_body",
    "prompt",
    "completion",
}


def registered_journal_events() -> frozenset[str]:
    return frozenset(_EVENT_REGISTRY)


def _safe_value(key: str, value: Any) -> Any:
    if key.lower() in _SENSITIVE or any(word in key.lower() for word in ("credential", "cookie")):
        return "<redacted>"
    if isinstance(value, str):
        return trim_detail(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {
            str(k): _safe_value(str(k), v) for k, v in value.items() if str(k).lower() != "text"
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(key, item) for item in value]
    return f"<{type(value).__name__}>"


class ProviderJournal:
    def __init__(
        self,
        meeting_id: str,
        *,
        emit: Callable[[str], None] | None = None,
        now: Callable[[], datetime] | None = None,
        path: str | Path | None = None,
        stdout: bool = True,
        max_bytes: int = 1_048_576,
    ) -> None:
        self._meeting_id = meeting_id
        self._emit = emit
        self._stdout = stdout
        self._now = now or (lambda: datetime.now(UTC))
        self._path = Path(path) if path is not None else None
        self._max_bytes = max(0, max_bytes)
        self._file_handler: RotatingFileHandler | None = None
        self._started: dict[str, float] = {}
        self._lock = Lock()
        if self._path is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(
                    self._path,
                    maxBytes=max(1, self._max_bytes),
                    backupCount=JOURNAL_BACKUP_COUNT,
                    encoding="utf-8",
                )
                handler.setFormatter(logging.Formatter("%(message)s"))
                self._file_handler = handler
            except BaseException:
                self._file_handler = None

    def record(self, event: str, **fields: Any) -> str | None:
        try:
            _EVENT_REGISTRY.add(event)
            at = self._now().isoformat()
            row: dict[str, Any] = {
                "event": event,
                "meeting_id": self._meeting_id,
                "at": at,
            }
            row.update(_safe_value("", fields))
            line = _encode_bounded(row, self._max_bytes)
            with self._lock:
                if self._file_handler is not None:
                    try:
                        self._file_handler.emit(
                            logging.LogRecord(
                                "fakeyou.journal",
                                logging.INFO,
                                __file__,
                                0,
                                line.rstrip("\n"),
                                (),
                                None,
                            )
                        )
                    except BaseException:
                        pass
                try:
                    if self._emit is not None:
                        self._emit(line.rstrip("\n"))
                    elif self._stdout:
                        print(line, end="", flush=True)
                except BaseException:
                    pass
            return at
        except BaseException:
            return None

    def close(self) -> None:
        with self._lock:
            handler = self._file_handler
            self._file_handler = None
            if handler is not None:
                try:
                    handler.close()
                except BaseException:
                    pass

    def request_started(
        self, request_id: str | None = None, *, source: str, operation: str, **fields: Any
    ) -> str:
        identifier = request_id or uuid.uuid4().hex
        self._started[identifier] = time.monotonic()
        self.record(
            "request_started", request_id=identifier, source=source, operation=operation, **fields
        )
        return identifier

    def request_finished(
        self, request_id: str, *, elapsed_ms: float | None = None, **fields: Any
    ) -> None:
        elapsed = elapsed_ms if elapsed_ms is not None else self._elapsed(request_id)
        self.record("request_finished", request_id=request_id, elapsed_ms=elapsed, **fields)
        self._started.pop(request_id, None)

    def request_failed(
        self, request_id: str, *, elapsed_ms: float | None = None, **fields: Any
    ) -> None:
        elapsed = elapsed_ms if elapsed_ms is not None else self._elapsed(request_id)
        self.record("request_failed", request_id=request_id, elapsed_ms=elapsed, **fields)
        self._started.pop(request_id, None)

    def _elapsed(self, request_id: str) -> float | None:
        started = self._started.get(request_id)
        return None if started is None else round((time.monotonic() - started) * 1000, 3)


def _encode_bounded(row: dict[str, Any], max_bytes: int) -> str:
    line = json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n"
    if max_bytes <= 0 or len(line.encode()) <= max_bytes:
        return line
    compact = dict(row)
    for key in ("detail", "error", "operation", "source", "request_id"):
        compact.pop(key, None)
        line = json.dumps(compact, separators=(",", ":"), allow_nan=False) + "\n"
        if len(line.encode()) <= max_bytes:
            return line
    minimal = {key: compact[key] for key in ("event", "meeting_id", "at") if key in compact}
    line = json.dumps(minimal, separators=(",", ":"), allow_nan=False) + "\n"
    return line if len(line.encode()) <= max_bytes else "{}\n"[:max_bytes]
