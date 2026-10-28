import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

Row = dict[str, Any]


def summarize(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None}
    p95 = (
        statistics.quantiles(values, n=20, method="inclusive")[18] if len(values) > 1 else values[0]
    )
    return {
        "count": len(values),
        "p50": round(statistics.median(values), 3),
        "p95": round(p95, 3),
        "max": round(max(values), 3),
    }


def _at(row: Row) -> datetime | None:
    value = row.get("at")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _ms(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() * 1000


def _timed(rows: Iterable[Row]) -> list[tuple[datetime, Row]]:
    return [(at, row) for row in rows if (at := _at(row)) is not None]


def _vad_stop_to_final(timed: Sequence[tuple[datetime, Row]]) -> list[float]:
    stopped: datetime | None = None
    gaps: list[float] = []
    for at, row in timed:
        event = row.get("event")
        if event == "speech_stopped":
            stopped = at
        elif event == "transcript_finalized" and stopped is not None:
            gaps.append(_ms(stopped, at))
            stopped = None
    return gaps


def _final_to_first_audio(timed: Sequence[tuple[datetime, Row]]) -> list[float]:
    finals: dict[str, datetime] = {}
    gaps: list[float] = []
    for at, row in timed:
        transcript_id = row.get("transcript_id")
        if not isinstance(transcript_id, str):
            continue
        if row.get("event") == "transcript_finalized":
            finals.setdefault(transcript_id, at)
        elif row.get("event") == "playback_started" and transcript_id in finals:
            gaps.append(_ms(finals.pop(transcript_id), at))
    return gaps


def _final_to_first_pcm(timed: Sequence[tuple[datetime, Row]]) -> list[float]:
    finals: dict[str, datetime] = {}
    gaps: list[float] = []
    for at, row in timed:
        transcript_id = row.get("transcript_id")
        if not isinstance(transcript_id, str):
            continue
        if row.get("event") == "transcript_finalized":
            finals.setdefault(transcript_id, at)
        elif row.get("event") == "synthesis_pcm_emitted" and transcript_id in finals:
            gaps.append(_ms(finals.pop(transcript_id), at))
    return gaps


def _sentence_gaps(timed: Sequence[tuple[datetime, Row]]) -> list[float]:
    finished: dict[tuple[str, int], datetime] = {}
    gaps: list[float] = []
    for at, row in timed:
        transcript_id, index = row.get("transcript_id"), row.get("sentence_index")
        if not isinstance(transcript_id, str) or not isinstance(index, int):
            continue
        if row.get("event") == "playback_finished":
            finished[(transcript_id, index)] = at
        elif row.get("event") == "playback_started" and (transcript_id, index - 1) in finished:
            gaps.append(_ms(finished.pop((transcript_id, index - 1)), at))
    return gaps


def _barge_in_to_silence(timed: Sequence[tuple[datetime, Row]]) -> list[float]:
    onset: datetime | None = None
    barged: datetime | None = None
    gaps: list[float] = []
    for at, row in timed:
        event = row.get("event")
        if event == "speech_started":
            onset = at
        elif event == "barge_in_detected":
            barged = onset or at
        elif event == "playback_dropped" and row.get("detail") == "interrupted" and barged:
            gaps.append(_ms(barged, at))
            barged = None
    return gaps


def _share(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "count": numerator,
        "of": denominator,
        "ratio": round(numerator / denominator, 3) if denominator else None,
    }


def baseline_metrics(rows: Sequence[Row]) -> dict[str, Any]:
    timed = _timed(rows)
    events = Counter(str(row.get("event")) for row in rows)
    finals = [row for row in rows if row.get("event") == "transcript_finalized"]
    plans = [row for row in rows if row.get("event") == "reply_plan_observed"]
    rejected = Counter(
        str(row.get("reason")) for row in rows if row.get("event") == "speech_rejected"
    )
    finished = next((row for row in reversed(rows) if row.get("event") == "session_finished"), None)
    return {
        "vad_stop_to_final_ms": summarize(_vad_stop_to_final(timed)),
        "final_to_first_pcm_ms": summarize(_final_to_first_pcm(timed)),
        "final_to_first_audio_ms": summarize(_final_to_first_audio(timed)),
        "barge_in_to_silence_ms": summarize(_barge_in_to_silence(timed)),
        "sentence_gap_ms": summarize(_sentence_gaps(timed)),
        "speaker_bound": _share(sum(1 for row in finals if row.get("speaker_bound")), len(finals)),
        "contributions": _share(
            sum(1 for row in plans if row.get("contribution") not in (None, "none")), len(plans)
        ),
        "speech_rejected": dict(rejected),
        "barge_in_rejected": events["barge_in_rejected"],
        "early_turn_started": events["early_turn_started"],
        "screen_reads": events["screen_reading_finished"],
        "screen_duplicates": events["screen_duplicate_detected"],
        "memory_prefetches": sum(
            1
            for row in rows
            if row.get("event") == "request_started"
            and row.get("source") == "memory"
            and row.get("operation") == "prefetch"
        ),
        "terminal_reason": finished.get("terminal_reason") if finished else None,
        "terminal_detail": finished.get("detail") if finished else None,
    }
