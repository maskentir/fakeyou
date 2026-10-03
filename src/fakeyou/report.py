import json
import math
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fakeyou.journal import JOURNAL_BACKUP_COUNT

LATENCY_STAGES = (
    "speech_stopped_to_final_seconds",
    "final_to_agent_seconds",
    "agent_to_reply_seconds",
    "final_to_reply_seconds",
    "reply_to_first_pcm_seconds",
    "speech_stopped_to_first_pcm_seconds",
    "agent_to_first_text_seconds",
    "agent_to_first_sentence_seconds",
    "first_sentence_to_first_pcm_seconds",
)
_STAGE_BOUNDS = {
    "speech_stopped_to_final_seconds": ("speech_stopped_at", "final_at"),
    "final_to_agent_seconds": ("final_at", "agent_at"),
    "agent_to_reply_seconds": ("agent_at", "reply_at"),
    "final_to_reply_seconds": ("final_at", "reply_at"),
    "reply_to_first_pcm_seconds": ("reply_at", "first_pcm_at"),
    "speech_stopped_to_first_pcm_seconds": ("speech_stopped_at", "first_pcm_at"),
}
_STREAM_STAGE_BOUNDS = {
    "agent_to_first_text_seconds": ("agent_at", "first_text_at"),
    "agent_to_first_sentence_seconds": ("agent_at", "first_sentence_at"),
    "first_sentence_to_first_pcm_seconds": ("first_sentence_at", "first_pcm_at"),
}
STARTUP_MILESTONES = (
    "audio_opened",
    "join_requested",
    "join_finished",
    "microphone_enabled",
    "conversation_bound",
)


def read_journal(path: Path) -> list[dict[str, Any]]:
    files = [Path(f"{path}.{index}") for index in range(JOURNAL_BACKUP_COUNT, 0, -1)] + [path]
    rows: list[dict[str, Any]] = []
    for file in files:
        if not file.exists():
            continue
        for line in file.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


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


def build_session_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    turns: dict[str, dict[str, Any]] = {}
    speech_stopped_at: datetime | None = None
    speech_started_at: datetime | None = None
    speak_started_at: datetime | None = None
    interruption_seconds: list[float] = []
    barge_in_triggers = Counter[str]()
    active: dict[str, Any] | None = None
    replying: dict[str, Any] | None = None
    synthesis_turns: dict[str, str | None] = {}
    pending: dict[str, Any] = {}
    excluded = Counter[str]()
    replies = Counter[str]()
    tools: dict[str, Counter[str]] = {}
    episodes: list[dict[str, Any]] = []
    faults = Counter[tuple[str, str]]()
    result: dict[str, Any] | None = None
    milestones: dict[str, dict[str, Any]] = {}
    cancel_requested = 0
    cancel_completed = 0
    ingest_runs: list[tuple[bool | None, bool]] = []
    ingest_fault_pending = False
    utterances = 0
    attributed = 0
    unknown_reasons = Counter[str]()
    policy_actions = Counter[str]()
    policy_reasons = Counter[str]()
    observer_methods = Counter[str]()
    for row in rows:
        event = row.get("event")
        try:
            at = datetime.fromisoformat(row["at"]) if "at" in row else None
        except (TypeError, ValueError):
            at = None
        if event in STARTUP_MILESTONES and row.get("source") == "runtime":
            milestones.setdefault(str(event), {**row, "at": at})
        if event == "speech_stopped":
            speech_stopped_at = at
        elif event == "speech_started":
            speech_started_at = at
        elif event == "barge_in_detected":
            barge_in_triggers[str(row.get("trigger"))] += 1
        elif event == "transcript_finalized":
            utterances += 1
            speaker_id = row.get("participant_id")
            if isinstance(speaker_id, str) and speaker_id != "unknown":
                attributed += 1
            elif row.get("speaker_evidence_id") is None:
                unknown_reasons["no_evidence"] += 1
            else:
                unknown_reasons["evidence_unmatched"] += 1
            transcript_id = str(row.get("transcript_id"))
            turns[transcript_id] = {
                "transcript_id": transcript_id,
                "speech_stopped_at": speech_stopped_at,
                "final_at": at,
                "tools": [],
                "outcome": "pending",
                "llm_count": 0,
                "llm_ms": 0.0,
                "recall_count": 0,
                "recall_ms": 0.0,
                "incomplete": 0,
                "sentence_count": 0,
                "streamed": False,
            }
            speech_stopped_at = None
        elif event == "agent_started":
            active = _turn_for(turns, row)
            if active is not None:
                active["agent_at"] = at
        elif event == "request_started":
            if row.get("source") == "llm":
                request_id = row.get("request_id")
                if isinstance(request_id, str):
                    pending[request_id] = row.get("transcript_id")
        elif event in ("request_finished", "request_failed"):
            if row.get("source") == "llm":
                request_id = row.get("request_id")
                if isinstance(request_id, str):
                    pending.pop(request_id, None)
                turn = _transcript_turn(turns, row, excluded)
                if turn is not None:
                    turn["llm_count"] += 1
                    _add_duration(turn, "llm_ms", row.get("duration_ms"), excluded)
        elif event == "tool_started":
            request_id = row.get("request_id")
            if isinstance(request_id, str):
                pending[request_id] = row.get("transcript_id")
        elif event in ("tool_finished", "tool_failed"):
            name = str(row.get("tool"))
            outcome = "finished" if event == "tool_finished" else "failed"
            tools.setdefault(name, Counter())[outcome] += 1
            request_id = row.get("request_id")
            if isinstance(request_id, str):
                pending.pop(request_id, None)
            tool_transcript = row.get("transcript_id")
            if isinstance(tool_transcript, str):
                turn = turns.get(tool_transcript)
                if turn is None:
                    excluded["unknown_transcript"] += 1
                else:
                    turn["tools"].append(name)
                    if name == "recall_meeting_context":
                        turn["recall_count"] += 1
                        _add_duration(turn, "recall_ms", row.get("duration_ms"), excluded)
            else:
                excluded["missing_transcript"] += 1
                if active is not None:
                    active["tools"].append(name)
        elif event == "agent_finished":
            replying = _turn_for(turns, row)
            if replying is not None:
                replying["reply_at"] = at
                replying["outcome"] = "silent"
        elif event in ("agent_cancelled", "agent_failed"):
            if event == "agent_cancelled":
                cancel_completed += 1
            turn = _turn_for(turns, row)
            if turn is not None:
                turn["outcome"] = "cancelled" if event == "agent_cancelled" else "failed"
        elif event == "agent_cancel_requested":
            cancel_requested += 1
        elif event == "answer_text_emitted":
            turn = _transcript_turn(turns, row, excluded)
            if turn is not None:
                turn["streamed"] = True
                if at is not None and turn.get("first_text_at") is None:
                    turn["first_text_at"] = at
        elif event == "answer_sentence_emitted":
            turn = _transcript_turn(turns, row, excluded)
            if turn is not None:
                turn["streamed"] = True
                if at is not None and turn.get("first_sentence_at") is None:
                    turn["first_sentence_at"] = at
                turn["sentence_count"] += 1
        elif event == "playback_started":
            speak_started_at = at
            if replying is not None:
                replying.setdefault("speak_at", at)
        elif event == "synthesis_started":
            request_id = row.get("request_id")
            if isinstance(request_id, str):
                linked = row.get("transcript_id")
                if isinstance(linked, str) and linked in turns:
                    if synthesis_turns.get(request_id, linked) != linked:
                        synthesis_turns[request_id] = None
                    else:
                        synthesis_turns[request_id] = linked
                else:
                    if isinstance(linked, str):
                        excluded["unknown_transcript"] += 1
                    if request_id in synthesis_turns:
                        synthesis_turns[request_id] = None
                    else:
                        synthesis_turns[request_id] = (
                            replying["transcript_id"] if replying is not None else None
                        )
        elif event == "synthesis_pcm_emitted":
            turn = _synthesis_turn(turns, synthesis_turns, row, excluded)
            if turn is not None and at is not None:
                turn.setdefault("first_pcm_at", at)
        elif event == "synthesis_finished":
            if not isinstance(row.get("request_id"), str):
                if replying is not None and "speak_at" in replying:
                    pcm_at = _first_pcm_at(row, at, excluded)
                    if pcm_at is not None:
                        replying.setdefault("first_pcm_at", pcm_at)
            else:
                turn = _synthesis_turn(turns, synthesis_turns, row, excluded)
                if turn is not None:
                    pcm_at = _first_pcm_at(row, at, excluded)
                    if pcm_at is not None:
                        turn.setdefault("first_pcm_at", pcm_at)
        elif event == "playback_finished":
            replies["spoken"] += 1
            replies["trailing_questions"] += row.get("ends_with_question") is True
            if replying is not None:
                replying["outcome"] = "spoken"
            replying = None
        elif event == "playback_dropped":
            replies["interrupted"] += 1
            if (
                row.get("detail") == "interrupted"
                and at is not None
                and speech_started_at is not None
                and speak_started_at is not None
                and speech_started_at >= speak_started_at
            ):
                interruption_seconds.append((at - speech_started_at).total_seconds())
            if replying is not None:
                replying["outcome"] = "interrupted"
            replying = None
        elif event in ("memory_episode_ingested", "memory_episode_failed"):
            episodes.append(row)
        elif event == "memory_ingest_finished":
            raw_completed = row.get("completed")
            ingest_runs.append(
                (
                    bool(raw_completed) if isinstance(raw_completed, bool) else None,
                    ingest_fault_pending,
                )
            )
            ingest_fault_pending = False
        elif event == "fault_recorded":
            faults[(str(row.get("source")), str(row.get("kind")))] += 1
            if row.get("source") == "memory_ingest":
                ingest_fault_pending = True
        elif event == "session_finished":
            result = {key: row.get(key) for key in ("phase", "terminal_reason", "exit_code")}
        elif event == "policy_decided":
            policy_actions[str(row.get("action"))] += 1
            policy_reasons[str(row.get("reason"))] += 1
        elif event == "participant_observer_started":
            observer_methods[str(row.get("method"))] += 1
    for transcript_id in pending.values():
        if isinstance(transcript_id, str) and transcript_id in turns:
            turns[transcript_id]["incomplete"] += 1
        elif isinstance(transcript_id, str):
            excluded["unknown_transcript"] += 1
        else:
            excluded["missing_transcript"] += 1
    turn_reports = [_turn_report(turn, excluded) for turn in turns.values()]
    if result is None:
        session_outcome = "unknown"
    elif result.get("exit_code") is None:
        session_outcome = "unknown"
    elif result.get("exit_code") == 0:
        session_outcome = "completed"
    else:
        session_outcome = "failed"
    if not ingest_runs:
        ingestion_outcome = "unknown"
    else:
        run_completed, run_faulted = ingest_runs[-1]
        if run_completed is None:
            ingestion_outcome = "unknown"
        elif run_completed:
            ingestion_outcome = "completed"
        elif run_faulted:
            ingestion_outcome = "failed"
        else:
            ingestion_outcome = "interrupted"
    return {
        "startup": _startup_report(milestones),
        "session_outcome": session_outcome,
        "ingestion_outcome": ingestion_outcome,
        "cancellation": {"requested": cancel_requested, "completed": cancel_completed},
        "participation": {
            "utterances_recorded": utterances,
            "attributed_utterances": attributed,
            "attribution_coverage": round(attributed / utterances, 3) if utterances else None,
            "unknown_reasons": dict(unknown_reasons),
            "answers": sum(1 for turn in turn_reports if turn["llm_count"] > 0),
            "policy": dict(policy_actions),
            "policy_reasons": dict(policy_reasons),
            "observer_methods": dict(observer_methods),
        },
        "turns": turn_reports,
        "latency": {
            stage: summarize([turn[stage] for turn in turn_reports if turn[stage] is not None])
            for stage in LATENCY_STAGES
        },
        "evidence": {"excluded": dict(excluded)},
        "replies": {key: replies[key] for key in ("spoken", "interrupted", "trailing_questions")},
        "tools": {name: dict(counts) for name, counts in tools.items()},
        "interruptions": {
            "triggers": dict(barge_in_triggers),
            "speech_to_stop_seconds": summarize(interruption_seconds),
        },
        "ingestion": {
            "episodes": len(episodes),
            "facts": sum(int(episode.get("facts") or 0) for episode in episodes),
            "empty": sum(
                episode["event"] == "memory_episode_ingested" and not episode.get("facts")
                for episode in episodes
            ),
            "failed": sum(episode["event"] == "memory_episode_failed" for episode in episodes),
            "duration_seconds": summarize(
                [float(episode.get("duration_ms") or 0) / 1000 for episode in episodes]
            ),
        },
        "faults": [
            {"source": source, "kind": kind, "count": count}
            for (source, kind), count in faults.items()
        ],
        "result": result,
    }


def _startup_report(milestones: dict[str, dict[str, Any]]) -> dict[str, Any]:
    join = milestones.get("join_finished", {})
    admitted_at = join.get("at") if join.get("status") == "admitted" else None

    def since_admission(event: str) -> float | None:
        at = milestones.get(event, {}).get("at")
        return round((at - admitted_at).total_seconds(), 3) if at and admitted_at else None

    def seconds(row: dict[str, Any]) -> float | None:
        return round(float(row["duration_ms"]) / 1000, 3) if "duration_ms" in row else None

    return {
        "audio_startup_seconds": seconds(milestones.get("audio_opened", {})),
        "join_status": join.get("status"),
        "lobby_wait_seconds": seconds(join),
        "audio_ready_at_admission": join.get("audio_ready") if admitted_at else None,
        "admission_to_microphone_seconds": since_admission("microphone_enabled"),
        "admission_to_listening_seconds": since_admission("conversation_bound"),
    }


def _turn_for(turns: dict[str, dict[str, Any]], row: dict[str, Any]) -> dict[str, Any] | None:
    transcript_id = str(row.get("request_id", "")).rpartition(":")[0]
    return turns.get(transcript_id)


def _transcript_turn(
    turns: dict[str, dict[str, Any]], row: dict[str, Any], excluded: Counter[str]
) -> dict[str, Any] | None:
    transcript_id = row.get("transcript_id")
    if not isinstance(transcript_id, str):
        excluded["missing_transcript"] += 1
        return None
    turn = turns.get(transcript_id)
    if turn is None:
        excluded["unknown_transcript"] += 1
        return None
    return turn


def _add_duration(turn: dict[str, Any], key: str, value: Any, excluded: Counter[str]) -> None:
    if value is None:
        return
    kind = _duration_kind(value)
    if kind == "nonfinite":
        excluded["nonfinite_duration"] += 1
    elif kind == "invalid":
        excluded["invalid_duration"] += 1
    else:
        turn[key] += float(value)


def _synthesis_turn(
    turns: dict[str, dict[str, Any]],
    synthesis_turns: dict[str, str | None],
    row: dict[str, Any],
    excluded: Counter[str],
) -> dict[str, Any] | None:
    request_id = row.get("request_id")
    if not isinstance(request_id, str) or request_id not in synthesis_turns:
        excluded["missing_synthesis_start"] += 1
        return None
    transcript_id = synthesis_turns[request_id]
    turn = turns.get(transcript_id) if transcript_id is not None else None
    if turn is None:
        excluded["ambiguous_synthesis_request"] += 1
        return None
    return turn


def _duration_kind(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "invalid"
    if not math.isfinite(value):
        return "nonfinite"
    if value < 0:
        return "invalid"
    return "valid"


def _first_pcm_at(
    row: dict[str, Any], finished_at: datetime | None, excluded: Counter[str]
) -> datetime | None:
    if finished_at is None:
        return None
    first_chunk_ms = row.get("first_chunk_ms")
    duration_ms = row.get("duration_ms")
    if first_chunk_ms is None or duration_ms is None:
        return None
    kinds = {_duration_kind(first_chunk_ms), _duration_kind(duration_ms)}
    if "nonfinite" in kinds:
        excluded["nonfinite_duration"] += 1
        return None
    if "invalid" in kinds or float(first_chunk_ms) > float(duration_ms):
        excluded["invalid_duration"] += 1
        return None
    started = finished_at - timedelta(milliseconds=float(duration_ms))
    return started + timedelta(milliseconds=float(first_chunk_ms))


def _turn_report(turn: dict[str, Any], excluded: Counter[str]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "transcript_id": turn["transcript_id"],
        "outcome": turn["outcome"],
        "tools": turn["tools"],
        "llm_count": turn["llm_count"],
        "recall_count": turn["recall_count"],
        "llm_duration_seconds": round(turn["llm_ms"] / 1000, 3),
        "recall_duration_seconds": round(turn["recall_ms"] / 1000, 3),
        "incomplete_request_count": turn["incomplete"],
        "sentence_count": turn["sentence_count"],
        "streamed": turn["streamed"],
    }
    for stage, (start_key, end_key) in _STAGE_BOUNDS.items():
        start, end = turn.get(start_key), turn.get(end_key)
        if start is None or end is None:
            excluded["missing_timestamp"] += 1
            report[stage] = None
            continue
        delta = (end - start).total_seconds()
        if delta < 0:
            excluded["negative_latency"] += 1
            report[stage] = None
            continue
        report[stage] = round(delta, 3)
    for stage, (start_key, end_key) in _STREAM_STAGE_BOUNDS.items():
        if not turn["streamed"]:
            report[stage] = None
            continue
        start, end = turn.get(start_key), turn.get(end_key)
        if start is None or end is None:
            excluded["missing_timestamp"] += 1
            report[stage] = None
            continue
        delta = (end - start).total_seconds()
        if delta < 0:
            excluded["negative_latency"] += 1
            report[stage] = None
            continue
        report[stage] = round(delta, 3)
    return report
