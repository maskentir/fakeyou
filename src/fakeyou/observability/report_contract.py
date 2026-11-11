from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fakeyou.foundation.timeparse import parse_iso_datetime
from fakeyou.observability.baseline import summarize

RESPONSE_STAGE = "speech_stopped_to_first_audio_seconds"
ANSWER_STAGE = "speech_stopped_to_first_answer_audio_seconds"


@dataclass(slots=True)
class _Turn:
    transcript_id: str
    stopped_at: datetime | None
    expected: bool = False
    silent: bool = False
    cancelled: bool = False
    failed: bool = False
    plan_speech: bool | None = None
    evidence: tuple[str, ...] = ()
    response_at: datetime | None = None
    answer_at: datetime | None = None
    spoken: list[tuple[bool, bool, bool]] = field(default_factory=list)
    interrupted: bool = False
    validated: dict[tuple[int, str], bool] = field(default_factory=dict)
    draft_intents: dict[str, bool] = field(default_factory=dict)
    accepted_drafts: set[str] = field(default_factory=set)


def _at(row: dict[str, Any]) -> datetime | None:
    value = row.get("at")
    return parse_iso_datetime(value) if isinstance(value, str) else None


def _identity(row: dict[str, Any]) -> str | None:
    identifier = row.get("transcript_id")
    if isinstance(identifier, str):
        return identifier
    if str(row.get("event", "")).startswith("agent_") or row.get("event") == "turn_suppressed":
        request = row.get("request_id")
        if isinstance(request, str):
            return request.rpartition(":")[0] or None
    return None


def _sentence_key(row: dict[str, Any]) -> tuple[int, str] | None:
    index, digest = row.get("sentence_index"), row.get("sentence_sha256")
    if type(index) is int and index >= 0 and isinstance(digest, str) and digest:
        return index, digest
    return None


def _observe(turn: _Turn, row: dict[str, Any]) -> None:
    event = row.get("event")
    if event in {"agent_started", "agent_failed", "playback_started", "state_answer_selected"}:
        turn.expected = True
    if event == "agent_started":
        turn.cancelled = False
    if event in {"agent_failed", "playback_failed"}:
        turn.failed = True
    if event == "agent_cancelled" or (event == "turn_suppressed" and row.get("reason") == "paused"):
        turn.cancelled = True
    if event in {"playback_audio_started", "thinking_cue_audio_started"}:
        _observe_audio(turn, row)
    _observe_plan(turn, row)
    _observe_sentence(turn, row)


def _observe_plan(turn: _Turn, row: dict[str, Any]) -> None:
    event, request_id = row.get("event"), str(row.get("request_id", ""))
    if event == "plan_selected":
        turn.plan_speech = row.get("output") == "speech" and row.get("template") not in {
            "silent",
            "action_only",
            "leave_only",
        }
        needs = row.get("evidence")
        if isinstance(needs, list):
            turn.evidence = tuple(sorted({need for need in needs if isinstance(need, str)}))
    if event == "reply_draft_accepted":
        turn.accepted_drafts.add(request_id)
        turn.silent = turn.draft_intents.get(request_id, False)
    if event == "reply_plan_observed":
        silent = row.get("intent") == "silent"
        if request_id.endswith(":draft"):
            turn.draft_intents[request_id] = silent
        if not request_id.endswith(":draft") or request_id in turn.accepted_drafts:
            turn.silent = silent


def _observe_sentence(turn: _Turn, row: dict[str, Any]) -> None:
    event = row.get("event")
    if event == "answer_sentence_accepted" and (key := _sentence_key(row)) is not None:
        turn.validated[key] = row.get("authorized_brief_question") is True
    if event == "playback_finished":
        trailing = row.get("ends_with_question") is True
        question = trailing or row.get("contains_question") is True
        key = _sentence_key(row)
        authorized = key is not None and turn.validated.get(key, False)
        turn.spoken.append((trailing, question, authorized))
    if event == "playback_dropped":
        turn.interrupted = True


def _expected(turn: _Turn) -> bool:
    if turn.answer_at is not None or turn.spoken:
        return True
    if turn.plan_speech is False:
        return False
    if turn.failed:
        return turn.expected
    return (turn.expected or turn.plan_speech is True) and not (turn.silent or turn.cancelled)


def _observe_audio(turn: _Turn, row: dict[str, Any]) -> None:
    at = _at(row)
    if at is None or turn.stopped_at is None or at < turn.stopped_at:
        return
    if turn.response_at is None or at < turn.response_at:
        turn.response_at = at
    if row.get("event") == "playback_audio_started":
        turn.expected = True
        if turn.answer_at is None or at < turn.answer_at:
            turn.answer_at = at


def _latency(turn: _Turn, at: datetime | None) -> float | None:
    if turn.stopped_at is None or at is None:
        return None
    return round((at - turn.stopped_at).total_seconds(), 3)


def _evidence_latencies(turns: list[_Turn]) -> dict[str, Any]:
    groups: dict[str, list[_Turn]] = {}
    for turn in turns:
        groups.setdefault("+".join(turn.evidence) or "none", []).append(turn)
    return {
        kind: {
            "expected_answers": len(group),
            "first_audio_seconds": summarize(
                [value for turn in group if (value := _latency(turn, turn.response_at)) is not None]
            ),
            "first_answer_audio_seconds": summarize(
                [value for turn in group if (value := _latency(turn, turn.answer_at)) is not None]
            ),
        }
        for kind, group in sorted(groups.items())
    }


def measures_output(rows: list[dict[str, Any]]) -> bool:
    return any(row.get("answer_boundary") is True for row in rows)


EVIDENCE_PATHS = ("no_visual", "visual_cached", "visual_fresh")
EVIDENCE_PATHS_V2 = ("no_visual", "visual_cached", "visual_fresh", "visual_unavailable")
VISUAL_NEEDS = frozenset({"screen", "participant"})


def _evidence_resolutions(rows: list[dict[str, Any]]) -> dict[str, str]:
    resolutions: dict[str, str] = {}
    for row in rows:
        if row.get("event") != "perception_evidence_resolved":
            continue
        transcript_id = row.get("transcript_id")
        resolution = row.get("resolution")
        if isinstance(transcript_id, str) and transcript_id:
            if resolution == "cache_hit":
                resolutions[transcript_id] = "visual_cached"
            elif resolution == "fresh":
                resolutions[transcript_id] = "visual_fresh"
    return resolutions


def _path_of(turn: _Turn, resolutions: dict[str, str]) -> str:
    if turn.transcript_id in resolutions:
        return resolutions[turn.transcript_id]
    if "screen" in turn.evidence:
        return "visual_cached"
    return "no_visual"


def _evidence_observations(rows: list[dict[str, Any]]) -> dict[str, list[tuple[str, str]]]:
    observations: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        if row.get("event") != "perception_evidence_resolved":
            continue
        transcript_id = row.get("transcript_id")
        kind = row.get("evidence_kind")
        resolution = row.get("resolution")
        if isinstance(transcript_id, str) and transcript_id and isinstance(kind, str):
            observations.setdefault(transcript_id, []).append((kind, str(resolution)))
    return observations


def _classify_v2(turn: _Turn, observations: dict[str, list[tuple[str, str]]]) -> str:
    visual_requested = tuple(need for need in turn.evidence if need in VISUAL_NEEDS)
    seen = observations.get(turn.transcript_id, [])
    resolutions = [resolution for kind, resolution in seen if kind in VISUAL_NEEDS]
    if "fresh" in resolutions:
        return "visual_fresh"
    if "cache_hit" in resolutions:
        return "visual_cached"
    if visual_requested and resolutions and all(r == "unavailable" for r in resolutions):
        return "visual_unavailable"
    if visual_requested and len(resolutions) < len(visual_requested):
        return "unclassified"
    if visual_requested and not resolutions:
        return "unclassified"
    return "no_visual"


def _latency_by_path(turns: list[_Turn], resolutions: dict[str, str]) -> dict[str, Any]:
    groups: dict[str, list[_Turn]] = {path: [] for path in EVIDENCE_PATHS}
    for turn in turns:
        groups[_path_of(turn, resolutions)].append(turn)
    return {
        path: {
            "expected_answers": len(group),
            "first_audio_seconds": summarize(
                [value for turn in group if (value := _latency(turn, turn.response_at)) is not None]
            ),
            "first_answer_audio_seconds": summarize(
                [value for turn in group if (value := _latency(turn, turn.answer_at)) is not None]
            ),
        }
        for path, group in groups.items()
    }


def _latency_by_path_v2(
    turns: list[_Turn], observations: dict[str, list[tuple[str, str]]]
) -> dict[str, Any]:
    groups: dict[str, list[_Turn]] = {path: [] for path in EVIDENCE_PATHS_V2}
    groups["unclassified"] = []
    for turn in turns:
        groups[_classify_v2(turn, observations)].append(turn)
    return {
        path: {
            "expected_answers": len(group),
            "first_audio_seconds": summarize(
                [value for turn in group if (value := _latency(turn, turn.response_at)) is not None]
            ),
            "first_answer_audio_seconds": summarize(
                [value for turn in group if (value := _latency(turn, turn.answer_at)) is not None]
            ),
        }
        for path, group in groups.items()
    }


def _cache_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    hits = 0
    misses = 0
    ages: list[float] = []
    latencies: list[float] = []
    for row in rows:
        if row.get("event") != "perception_evidence_resolved":
            continue
        resolution = row.get("resolution")
        if resolution == "cache_hit":
            hits += 1
        elif resolution == "fresh":
            misses += 1
        age = row.get("age_ms")
        if isinstance(age, int):
            ages.append(float(age))
        latency = row.get("vision_latency_ms")
        if isinstance(latency, int):
            latencies.append(float(latency))
    total = hits + misses
    return {
        "hits": hits,
        "misses": misses,
        "hit_rate": round(hits / total, 3) if total else None,
        "age_ms": summarize(ages),
        "vision_latency_ms": summarize(latencies),
    }


def apply_contract(report: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    turns: dict[str, _Turn] = {}
    stopped_at: datetime | None = None
    faults: Counter[tuple[str, str, str | None]] = Counter()
    observes = measures_output(rows)
    for row in rows:
        event = row.get("event")
        if event == "fault_recorded":
            operation = row.get("operation")
            faults[
                (
                    str(row.get("source")),
                    str(row.get("kind")),
                    operation if isinstance(operation, str) else None,
                )
            ] += 1
        if event == "speech_stopped":
            stopped_at = _at(row)
        identifier = _identity(row)
        if event == "transcript_finalized" and identifier is not None:
            turns[identifier] = _Turn(identifier, stopped_at)
            stopped_at = None
        if identifier is not None and identifier in turns:
            _observe(turns[identifier], row)

    expected = [turn for turn in turns.values() if _expected(turn)] if observes else []
    spoken = [turn for turn in turns.values() if turn.spoken]
    response_values = [_latency(turn, turn.response_at) for turn in expected]
    answer_values = [_latency(turn, turn.answer_at) for turn in expected]
    for turn_report in report["turns"]:
        turn = turns.get(turn_report["transcript_id"])
        if turn is not None:
            turn_report[RESPONSE_STAGE] = _latency(turn, turn.response_at)
            turn_report[ANSWER_STAGE] = _latency(turn, turn.answer_at)
    report["latency_by_evidence"] = _evidence_latencies(expected)
    report["evidence_paths"] = any(row.get("evidence_paths") is True for row in rows)
    v2 = any(row.get("evidence_resolution_v2") is True for row in rows)
    report["evidence_resolution_v2"] = v2
    if v2:
        observations = _evidence_observations(rows)
        paths = _latency_by_path_v2(expected, observations)
        report["latency_by_evidence_path"] = {
            path: group for path, group in paths.items() if path in EVIDENCE_PATHS_V2
        }
        report["evidence_unclassified"] = paths.get("unclassified", {}).get("expected_answers", 0)
    else:
        resolutions = _evidence_resolutions(rows)
        report["latency_by_evidence_path"] = _latency_by_path(expected, resolutions)
    report["evidence_cache"] = _cache_report(rows)
    report["latency"][RESPONSE_STAGE] = summarize([v for v in response_values if v is not None])
    report["latency"][ANSWER_STAGE] = summarize([v for v in answer_values if v is not None])
    report["replies"] = {
        "spoken": len(spoken),
        "interrupted": sum(turn.interrupted for turn in turns.values()),
        "trailing_questions": sum(turn.spoken[-1][0] for turn in spoken),
        "trailing_question_rate": sum(turn.spoken[-1][0] for turn in spoken) / len(spoken)
        if spoken
        else None,
        "unnecessary_questions": (
            sum(
                any(question and not authorized for _, question, authorized in turn.spoken)
                for turn in spoken
            )
            if observes
            else None
        ),
        "expected_answers": len(expected),
        "answers_with_audio": (
            sum(value is not None for value in answer_values) if observes else None
        ),
    }
    report["faults"] = [
        {"source": source, "kind": kind, "operation": operation, "count": count}
        for (source, kind, operation), count in sorted(
            faults.items(), key=lambda item: tuple(str(value) for value in item[0])
        )
    ]
    return report
