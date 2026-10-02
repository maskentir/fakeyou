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


def attribute_speaker(
    window: SpeechWindow,
    samples: Sequence[SpeakerSample],
    roster: Mapping[str, Participant],
) -> Attribution:
    started = window.started_ms
    ended = window.ended_ms
    if not isfinite(started) or not isfinite(ended) or not ended > started:
        return _unknown("bad_clock")
    if started < 0 or ended < 0:
        return _unknown("bad_clock")
    duration = ended - started
    usable = sorted(
        {
            (sample.at_ms, sample.active_ids)
            for sample in samples
            if sample.epoch == window.epoch and isfinite(sample.at_ms)
        }
    )
    if not usable:
        return _unknown("no_samples")
    in_window = [(at, ids) for at, ids in usable if started <= at <= ended]
    if not in_window:
        return _unknown("unobserved_window")
    if any(len(ids) > 1 for _, ids in in_window):
        return _unknown("overlap")
    if any(len(ids) == 0 for _, ids in in_window):
        return _unknown("silence")
    by_moment: dict[float, set[str]] = {}
    for at, ids in in_window:
        by_moment.setdefault(at, set()).add(ids[0])
    if any(len(names) > 1 for names in by_moment.values()):
        return _unknown("conflict")
    first_id = in_window[0][1][0]
    if any(ids[0] != first_id for _, ids in in_window):
        return _unknown("speaker_switch")
    person = roster.get(first_id)
    if person is None or person.epoch != window.epoch:
        return _unknown("left_roster")
    if person.is_self:
        return _unknown("self")
    uncovered = max(0.0, in_window[0][0] - started)
    uncovered += max(0.0, ended - in_window[-1][0])
    for (previous, _), (current, _) in zip(in_window, in_window[1:], strict=False):
        if current - previous > MAX_SAMPLE_GAP_MS:
            return _unknown("stale_gap")
    coverage = max(0.0, 1.0 - uncovered / duration)
    if coverage < MIN_COVERAGE:
        return _unknown("low_coverage", coverage)
    return Attribution(first_id, person.display_name, "observed", "observed", coverage)
