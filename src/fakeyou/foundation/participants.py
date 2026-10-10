from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Literal


@dataclass(frozen=True, slots=True)
class Participant:
    id: str
    display_name: str
    epoch: str
    is_self: bool


@dataclass(frozen=True, slots=True)
class SpeakerSample:
    at_ms: float
    epoch: str
    active_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SpeechWindow:
    transcript_id: str
    epoch: str
    started_ms: float
    ended_ms: float


@dataclass(frozen=True, slots=True)
class Attribution:
    participant_id: str | None
    display_name: str | None
    status: Literal["observed", "unknown"]
    reason: str
    coverage: float


MIN_COVERAGE = 0.95
MAX_SAMPLE_GAP_MS = 500.0


def _unknown(reason: str, coverage: float = 0.0) -> Attribution:
    return Attribution(None, None, "unknown", reason, coverage)


def _clock_reason(window: SpeechWindow) -> str | None:
    started = window.started_ms
    ended = window.ended_ms
    if not isfinite(started) or not isfinite(ended) or not ended > started:
        return "bad_clock"
    if started < 0 or ended < 0:
        return "bad_clock"
    return None


def _windowed_samples(
    window: SpeechWindow, samples: Sequence[SpeakerSample]
) -> tuple[list[tuple[float, tuple[str, ...]]], str | None]:
    usable = sorted(
        {
            (sample.at_ms, sample.active_ids)
            for sample in samples
            if sample.epoch == window.epoch and isfinite(sample.at_ms)
        }
    )
    if not usable:
        return [], "no_samples"
    in_window = [(at, ids) for at, ids in usable if window.started_ms <= at <= window.ended_ms]
    if not in_window:
        return [], "unobserved_window"
    return in_window, None


def _consistency_reason(in_window: list[tuple[float, tuple[str, ...]]]) -> str | None:
    if any(len(ids) > 1 for _, ids in in_window):
        return "overlap"
    if any(len(ids) == 0 for _, ids in in_window):
        return "silence"
    by_moment: dict[float, set[str]] = {}
    for at, ids in in_window:
        by_moment.setdefault(at, set()).add(ids[0])
    if any(len(names) > 1 for names in by_moment.values()):
        return "conflict"
    first_id = in_window[0][1][0]
    if any(ids[0] != first_id for _, ids in in_window):
        return "speaker_switch"
    return None


def _roster_reason(person: Participant, window: SpeechWindow) -> str | None:
    if person.epoch != window.epoch:
        return "left_roster"
    if person.is_self:
        return "self"
    return None


def _coverage(
    window: SpeechWindow, in_window: list[tuple[float, tuple[str, ...]]]
) -> tuple[float, str | None]:
    started = window.started_ms
    ended = window.ended_ms
    uncovered = max(0.0, in_window[0][0] - started)
    uncovered += max(0.0, ended - in_window[-1][0])
    for (previous, _), (current, _) in zip(in_window, in_window[1:], strict=False):
        if current - previous > MAX_SAMPLE_GAP_MS:
            return 0.0, "stale_gap"
    coverage = max(0.0, 1.0 - uncovered / (ended - started))
    if coverage < MIN_COVERAGE:
        return coverage, "low_coverage"
    return coverage, None


def attribute_speaker(
    window: SpeechWindow,
    samples: Sequence[SpeakerSample],
    roster: Mapping[str, Participant],
) -> Attribution:
    reason = _clock_reason(window)
    if reason is not None:
        return _unknown(reason)
    in_window, reason = _windowed_samples(window, samples)
    if reason is not None:
        return _unknown(reason)
    reason = _consistency_reason(in_window)
    if reason is not None:
        return _unknown(reason)
    first_id = in_window[0][1][0]
    person = roster.get(first_id)
    if person is None:
        return _unknown("left_roster")
    reason = _roster_reason(person, window)
    if reason is not None:
        return _unknown(reason)
    coverage, reason = _coverage(window, in_window)
    if reason is not None:
        return _unknown(reason, coverage)
    return Attribution(first_id, person.display_name, "observed", "observed", coverage)
