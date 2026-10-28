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


def _aliases(rows: Iterable[Row]) -> dict[str, str]:
    return {
        str(row["early_transcript_id"]): str(row["transcript_id"])
        for row in rows
        if row.get("event") == "early_turn_verified"
        and row.get("early_transcript_id")
        and row.get("transcript_id")
    }


def _keyed(
    timed: Sequence[tuple[datetime, Row]], aliases: dict[str, str]
) -> list[tuple[datetime, Row]]:
    keyed: list[tuple[datetime, Row]] = []
    for at, row in timed:
        transcript_id = row.get("transcript_id")
        if isinstance(transcript_id, str) and transcript_id in aliases:
            row = {**row, "transcript_id": aliases[transcript_id]}
        keyed.append((at, row))
    return keyed


def _first_by_turn(timed: Sequence[tuple[datetime, Row]], event: str) -> dict[str, datetime]:
    first: dict[str, datetime] = {}
    for at, row in timed:
        transcript_id = row.get("transcript_id")
        if row.get("event") == event and isinstance(transcript_id, str):
            first.setdefault(transcript_id, at)
    return first


def _turn_gaps(start: dict[str, datetime], end: dict[str, datetime]) -> list[float]:
    return [_ms(start[key], at) for key, at in end.items() if key in start]


def _vad_stop_before_final(timed: Sequence[tuple[datetime, Row]]) -> dict[str, datetime]:
    stopped: datetime | None = None
    stops: dict[str, datetime] = {}
    for at, row in timed:
        event, transcript_id = row.get("event"), row.get("transcript_id")
        if event == "speech_stopped":
            stopped = at
        elif event == "transcript_finalized" and isinstance(transcript_id, str) and stopped:
            stops.setdefault(transcript_id, stopped)
            stopped = None
    return stops


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
    barged: dict[str, datetime] = {}
    gaps: list[float] = []
    for at, row in timed:
        event, transcript_id = row.get("event"), row.get("transcript_id")
        interrupted = row.get("interrupted_transcript_id")
        if event == "speech_started":
            onset = at
        elif event == "barge_in_detected" and isinstance(interrupted, str):
            barged.setdefault(interrupted, onset or at)
        elif (
            event == "playback_dropped"
            and row.get("detail") == "interrupted"
            and isinstance(transcript_id, str)
            and transcript_id in barged
        ):
            gaps.append(_ms(barged.pop(transcript_id), at))
    return gaps


def _share(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "count": numerator,
        "of": denominator,
        "ratio": round(numerator / denominator, 3) if denominator else None,
    }


def baseline_metrics(rows: Sequence[Row]) -> dict[str, Any]:
    timed = _keyed(_timed(rows), _aliases(rows))
    finals_at = _first_by_turn(timed, "transcript_finalized")
    audio_at = _first_by_turn(timed, "playback_started")
    stops_at = _vad_stop_before_final(timed)
    contributed = {
        str(row.get("transcript_id"))
        for _, row in timed
        if row.get("event") == "reply_plan_observed"
        and row.get("contribution") not in (None, "none")
    }
    events = Counter(str(row.get("event")) for row in rows)
    finals = [row for row in rows if row.get("event") == "transcript_finalized"]
    plans = [row for row in rows if row.get("event") == "reply_plan_observed"]
    rejected = Counter(
        str(row.get("reason")) for row in rows if row.get("event") == "speech_rejected"
    )
    finished = next((row for row in reversed(rows) if row.get("event") == "session_finished"), None)
    return {
        "vad_stop_to_final_ms": summarize(_turn_gaps(stops_at, finals_at)),
        "final_to_first_pcm_ms": summarize(
            _turn_gaps(finals_at, _first_by_turn(timed, "synthesis_pcm_emitted"))
        ),
        "final_to_first_audio_ms": summarize(_turn_gaps(finals_at, audio_at)),
        "vad_stop_to_first_audio_ms": summarize(_turn_gaps(stops_at, audio_at)),
        "barge_in_to_silence_ms": summarize(_barge_in_to_silence(timed)),
        "sentence_gap_ms": summarize(_sentence_gaps(timed)),
        "speaker_bound": _share(sum(1 for row in finals if row.get("speaker_bound")), len(finals)),
        "contributions_spoken": _share(len(contributed & audio_at.keys()), len(plans)),
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
