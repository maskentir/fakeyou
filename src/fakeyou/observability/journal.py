import json
import logging
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fakeyou.foundation.taxonomy import Subsystem
from fakeyou.observability.provider_faults import ProviderFault, trim_detail

JOURNAL_BACKUP_COUNT = 2
JOURNAL_MAX_BYTES = 1_048_576

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


def journal_path(directory: Path, meeting_id: str) -> Path:
    return directory / f"{meeting_id}.jsonl"


def journal_files(directory: Path, meeting_id: str) -> tuple[Path, ...]:
    path = journal_path(directory, meeting_id)
    return (path, *(Path(f"{path}.{index}") for index in range(1, JOURNAL_BACKUP_COUNT + 1)))


@dataclass(frozen=True, slots=True)
class JournalFile:
    path: str | Path
    max_bytes: int = JOURNAL_MAX_BYTES


@dataclass(slots=True)
class JournalTiming:
    fields: dict[str, Any]
    started_at: float

    @property
    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started_at) * 1000


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


class Journal:
    def __init__(
        self,
        meeting_id: str,
        *,
        emit: Callable[[str], None] | None = None,
        now: Callable[[], datetime] | None = None,
        file: JournalFile | None = None,
        echo: bool = True,
    ) -> None:
        self._meeting_id = meeting_id
        self._emit = emit
        self._echo = echo
        self._now = now or (lambda: datetime.now(UTC))
        self._path = Path(file.path) if file is not None else None
        self._max_bytes = max(0, file.max_bytes if file is not None else JOURNAL_MAX_BYTES)
        self._file_handler: RotatingFileHandler | None = None
        self._started: dict[str, JournalTiming] = {}
        self._logger = logging.Logger(f"fakeyou.observability.journal.{uuid.uuid4().hex}")
        self._logger.propagate = False
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
                self._logger.addHandler(handler)
            except BaseException:
                self._file_handler = None

    def record(self, event: str, *, source: Subsystem, **fields: Any) -> str | None:
        try:
            at = self._now().isoformat()
            row: dict[str, Any] = {
                "event": event,
                "meeting_id": self._meeting_id,
                "at": at,
                "source": source,
            }
            row.update(_safe_value("", fields))
            line = _encode_bounded(row, self._max_bytes)
            try:
                self._logger.info(line.rstrip("\n"))
            except BaseException:
                pass
            try:
                if self._emit is not None:
                    self._emit(line.rstrip("\n"))
                elif self._echo:
                    print(line, end="", flush=True, file=sys.stderr)
            except BaseException:
                pass
            return at
        except BaseException:
            return None

    def close(self) -> None:
        handler, self._file_handler = self._file_handler, None
        if handler is not None:
            self._logger.removeHandler(handler)
            try:
                handler.close()
            except BaseException:
                pass

    @contextmanager
    def timed(
        self,
        *,
        started: str | None = None,
        finished: str,
        failed: str,
        source: Subsystem,
        elapsed_key: str = "duration_ms",
        **fields: Any,
    ) -> Iterator[JournalTiming]:
        timing = JournalTiming(
            {"request_id": uuid.uuid4().hex, "source": source, **fields}, time.monotonic()
        )
        if started is not None:
            self.record(started, **timing.fields)
        try:
            yield timing
        except Exception as error:
            timing.fields.setdefault(elapsed_key, timing.elapsed_ms)
            if isinstance(error, ProviderFault):
                timing.fields["kind"] = error.kind
            timing.fields.setdefault("error", type(error).__name__)
            self.record(failed, **timing.fields)
            raise
        else:
            timing.fields.setdefault(elapsed_key, timing.elapsed_ms)
            self.record(finished, **timing.fields)

    def request_started(
        self, request_id: str | None = None, *, source: Subsystem, operation: str, **fields: Any
    ) -> str:
        identifier = request_id or uuid.uuid4().hex
        timing = JournalTiming(
            {"request_id": identifier, "source": source, "operation": operation}, time.monotonic()
        )
        self._started[identifier] = timing
        self.record("request_started", **{**timing.fields, **fields})
        return identifier

    def request_finished(self, request_id: str, **fields: Any) -> None:
        self._finish_request(request_id, "request_finished", fields)

    def request_failed(self, request_id: str, **fields: Any) -> None:
        self._finish_request(request_id, "request_failed", fields)

    def _finish_request(self, request_id: str, event: str, fields: dict[str, Any]) -> None:
        timing = self._started.pop(request_id, None)
        if timing is None:
            self.record(event, request_id=request_id, duration_ms=None, **fields)
            return
        self.record(event, **{**timing.fields, "duration_ms": timing.elapsed_ms, **fields})

    def meet_action_requested(
        self,
        *,
        action: str,
        action_id: str,
        actor: str,
        attempt: int = 1,
        chars: int | None = None,
        **fields: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "action": action,
            "action_id": action_id,
            "actor": actor,
            "attempt": attempt,
            **fields,
        }
        if chars is not None:
            payload["chars"] = chars
        self.record("meet_action_requested", source="agent", **payload)

    def meet_action_started(
        self, *, action: str, action_id: str, attempt: int = 1, **fields: Any
    ) -> None:
        self.record(
            "meet_action_started",
            source="agent",
            action=action,
            action_id=action_id,
            attempt=attempt,
            **fields,
        )

    def meet_action_observed(
        self,
        *,
        action: str,
        action_id: str,
        observed_state: str,
        chars: int | None = None,
        **fields: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "action": action,
            "action_id": action_id,
            "observed_state": observed_state,
            **fields,
        }
        if chars is not None:
            payload["chars"] = chars
        self.record("meet_action_observed", source="agent", **payload)

    def meet_action_finished(
        self,
        *,
        action: str,
        action_id: str,
        observed: bool = False,
        attempts: int = 1,
        duration_ms: float | None = None,
        **fields: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "action": action,
            "action_id": action_id,
            "observed": observed,
            "attempts": attempts,
            **fields,
        }
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        self.record("meet_action_finished", source="agent", **payload)

    def meet_action_failed(
        self,
        *,
        action: str,
        action_id: str,
        reason: str | None = None,
        attempts: int = 1,
        duration_ms: float | None = None,
        **fields: Any,
    ) -> None:
        payload: dict[str, Any] = {
            "action": action,
            "action_id": action_id,
            "attempts": attempts,
            **fields,
        }
        if reason is not None:
            payload["reason"] = reason
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        self.record("meet_action_failed", source="agent", **payload)

    def meet_action_rejected(
        self, *, action: str, action_id: str, reason: str, **fields: Any
    ) -> None:
        self.record(
            "meet_action_rejected",
            source="agent",
            action=action,
            action_id=action_id,
            reason=reason,
            **fields,
        )


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
