import asyncio
import json
import math
import traceback
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_events import JournalSink
from fakeyou.foundation.clock import monotonic
from fakeyou.foundation.resources import read_package_text
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.provider_faults import classify_exception

MAX_CONSECUTIVE_TURN_FAILURES = 3
FAILURE_APOLOGY_COOLDOWN_SECONDS = 60.0
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
TURN_FAILED_LINES: dict[str, str] = json.loads(read_package_text("spoken_lines.json"))[
    "turn_failed"
]


def _frame_label(frame: traceback.FrameSummary) -> str:
    return f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"


def raised_where(error: BaseException) -> dict[str, str]:
    frames = traceback.extract_tb(error.__traceback__)
    if not frames:
        return {}
    own = [frame for frame in frames if Path(frame.filename).is_relative_to(PACKAGE_ROOT)]
    origin = {"raised_at": _frame_label(frames[-1])}
    return origin | ({"raised_in": _frame_label(own[-1])} if own else {})


def turn_request_id(transcript_id: str, generation: int) -> str:
    return f"{transcript_id}:{generation}"


@dataclass(frozen=True, slots=True)
class TurnScope:
    request_id: str
    transcript_id: str
    generation: int
    started: float
    clock: Callable[[], float]
    response_timeout_seconds: float

    def duration_ms(self) -> float:
        return (self.clock() - self.started) * 1000


def record_agent_started(journal: JournalSink, scope: TurnScope) -> None:
    journal.record(
        "agent_started",
        source="agent",
        request_id=scope.request_id,
        transcript_id=scope.transcript_id,
    )


def record_agent_finished(journal: JournalSink, scope: TurnScope) -> None:
    journal.record(
        "agent_finished",
        source="agent",
        request_id=scope.request_id,
        transcript_id=scope.transcript_id,
        duration_ms=scope.duration_ms(),
    )


def record_agent_failed(journal: JournalSink, scope: TurnScope) -> None:
    journal.record(
        "agent_failed",
        source="agent",
        request_id=scope.request_id,
        duration_ms=scope.duration_ms(),
    )


def record_sentence_dropped(
    journal: JournalSink, scope: TurnScope | None, *, sentence_index: int, detail: str
) -> None:
    journal.record(
        "sentence_dropped",
        source="agent",
        request_id=None if scope is None else scope.request_id,
        transcript_id=None if scope is None else scope.transcript_id,
        sentence_index=sentence_index,
        detail=detail,
    )


def record_drop(
    journal: JournalSink,
    scope: TurnScope | None,
    *,
    sentence_index: int,
    detail: str,
    reason: str,
) -> None:
    record_sentence_dropped(journal, scope, sentence_index=sentence_index, detail=detail)
    if scope is not None:
        journal.record(
            "agent_cancelled",
            source="agent",
            request_id=scope.request_id,
            transcript_id=scope.transcript_id,
            generation=scope.generation,
            reason=reason,
            duration_ms=scope.duration_ms(),
        )


@dataclass(frozen=True, slots=True)
class RecoveryOptions:
    faults: FaultReporter | None = None
    language: str | Callable[[], str] = DEFAULTS.speech.language
    response_timeout_seconds: float = DEFAULTS.conversation.response_timeout_seconds

    def __post_init__(self) -> None:
        seconds = self.response_timeout_seconds
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("response timeout must be positive and finite")


class TurnRecovery:
    def __init__(
        self,
        *,
        journal: JournalSink,
        faults: FaultReporter | None,
        language: str | Callable[[], str],
        is_closed: Callable[[], bool],
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._journal = journal
        self._faults = faults
        self._is_closed = is_closed
        self._language = language
        self._clock = clock
        self._consecutive = 0
        self._apology_muted_until = 0.0

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive

    @property
    def failed_line(self) -> str:
        language = self._language() if callable(self._language) else self._language
        return TURN_FAILED_LINES.get(language, TURN_FAILED_LINES[DEFAULTS.speech.language])

    @property
    def should_apologize(self) -> bool:
        return self._clock() >= self._apology_muted_until

    def record_success(self) -> None:
        self._consecutive = 0
        self._apology_muted_until = 0.0

    def record_failure(self, error: Exception, transcript_id: str) -> bool:
        self._consecutive += 1
        self._journal.record(
            "turn_failed",
            source="agent",
            transcript_id=transcript_id,
            error=type(error).__name__,
            consecutive=self._consecutive,
            **raised_where(error),
        )
        if self._is_closed():
            return True
        if self._consecutive >= MAX_CONSECUTIVE_TURN_FAILURES:
            self._apology_muted_until = self._clock() + FAILURE_APOLOGY_COOLDOWN_SECONDS
        if self._faults is not None:
            self._faults.record(classify_exception("agent", error))
        return False


async def within_sentence_budget(
    stream: AsyncIterator[SpokenChunk], seconds: float
) -> AsyncIterator[SpokenChunk]:
    iterator = stream.__aiter__()
    while True:
        try:
            async with asyncio.timeout(seconds):
                sentence = await anext(iterator)
        except StopAsyncIteration:
            return
        yield sentence
