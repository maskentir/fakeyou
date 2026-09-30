import json
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fakeyou.journal import JOURNAL_BACKUP_COUNT

LATENCY_STAGES = (
    "speech_stopped_to_final_s",
    "final_to_reply_s",
    "reply_to_first_pcm_s",
    "speech_stopped_to_first_pcm_s",
)
_STAGE_BOUNDS = {
    "speech_stopped_to_final_s": ("speech_stopped_at", "final_at"),
    "final_to_reply_s": ("final_at", "reply_at"),
    "reply_to_first_pcm_s": ("reply_at", "first_pcm_at"),
    "speech_stopped_to_first_pcm_s": ("speech_stopped_at", "first_pcm_at"),
}
STARTUP_MILESTONES = (
    "audio_ready",
    "join_requested",
    "join_result",
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
    replies = Counter[str]()
    tools: dict[str, Counter[str]] = {}
    episodes: list[dict[str, Any]] = []
    faults = Counter[tuple[str, str]]()
    result: dict[str, Any] | None = None
    milestones: dict[str, dict[str, Any]] = {}
    for row in rows:
        event = row.get("event")
        at = datetime.fromisoformat(row["at"]) if "at" in row else None
        if event in STARTUP_MILESTONES and row.get("source") == "runtime":
            milestones.setdefault(str(event), {**row, "at": at})
        if event == "speech_stopped":
            speech_stopped_at = at
        elif event == "speech_started":
            speech_started_at = at
        elif event == "barge_in":
            barge_in_triggers[str(row.get("trigger"))] += 1
        elif event == "transcript_final":
            transcript_id = str(row.get("transcript_id"))
            turns[transcript_id] = {
                "transcript_id": transcript_id,
                "speech_stopped_at": speech_stopped_at,
                "final_at": at,
                "tools": [],
                "outcome": "pending",
            }
            speech_stopped_at = None
        elif event == "agent_started":
            active = _turn_for(turns, row)
        elif event in ("tool_finished", "tool_failed"):
            name = str(row.get("tool"))
            outcome = "finished" if event == "tool_finished" else "failed"
            tools.setdefault(name, Counter())[outcome] += 1
            if active is not None:
                active["tools"].append(name)
        elif event == "agent_finished":
            replying = _turn_for(turns, row)
            if replying is not None:
                replying["reply_at"] = at
                replying["outcome"] = "silent"
        elif event in ("agent_cancelled", "agent_failed"):
            turn = _turn_for(turns, row)
            if turn is not None:
                turn["outcome"] = "cancelled" if event == "agent_cancelled" else "failed"
        elif event == "speak_started":
            speak_started_at = at
            if replying is not None:
                replying.setdefault("speak_at", at)
        elif event == "synth_finished" and replying is not None and "speak_at" in replying:
            replying.setdefault("first_pcm_at", _first_pcm_at(row, at))
        elif event == "speak_finished":
            replies["spoken"] += 1
            replies["trailing_questions"] += str(row.get("text") or "").rstrip().endswith("?")
            if replying is not None:
                replying["outcome"] = "spoken"
            replying = None
        elif event == "speak_dropped":
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
        elif event == "memory_episode":
            episodes.append(row)
        elif event == "fault":
            faults[(str(row.get("source")), str(row.get("kind")))] += 1
        elif event == "session_result":
            result = {key: row.get(key) for key in ("phase", "terminal_reason", "exit_code")}
    turn_reports = [_turn_report(turn) for turn in turns.values()]
    return {
        "startup": _startup_report(milestones),
        "turns": turn_reports,
        "latency": {
            stage: summarize([turn[stage] for turn in turn_reports if turn[stage] is not None])
            for stage in LATENCY_STAGES
        },
        "replies": {key: replies[key] for key in ("spoken", "interrupted", "trailing_questions")},
        "tools": {name: dict(counts) for name, counts in tools.items()},
        "interruptions": {
            "triggers": dict(barge_in_triggers),
            "speech_to_stop_s": summarize(interruption_seconds),
        },
        "ingestion": {
            "episodes": len(episodes),
            "facts": sum(int(episode.get("facts") or 0) for episode in episodes),
            "empty": sum(episode.get("outcome") == "empty" for episode in episodes),
            "failed": sum(episode.get("outcome") == "failed" for episode in episodes),
            "duration_s": summarize(
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
    join = milestones.get("join_result", {})
    admitted_at = join.get("at") if join.get("status") == "admitted" else None

    def since_admission(event: str) -> float | None:
        at = milestones.get(event, {}).get("at")
        return round((at - admitted_at).total_seconds(), 3) if at and admitted_at else None

    def seconds(row: dict[str, Any]) -> float | None:
        return round(float(row["duration_ms"]) / 1000, 3) if "duration_ms" in row else None

    return {
        "audio_startup_s": seconds(milestones.get("audio_ready", {})),
        "join_status": join.get("status"),
        "lobby_wait_s": seconds(join),
        "audio_ready_at_admission": join.get("audio_ready") if admitted_at else None,
        "admission_to_microphone_s": since_admission("microphone_enabled"),
        "admission_to_listening_s": since_admission("conversation_bound"),
    }


def _turn_for(turns: dict[str, dict[str, Any]], row: dict[str, Any]) -> dict[str, Any] | None:
    transcript_id = str(row.get("request_id", "")).rpartition(":")[0]
    return turns.get(transcript_id)


def _first_pcm_at(row: dict[str, Any], finished_at: datetime | None) -> datetime | None:
    if finished_at is None:
        return None
    first_chunk_ms = row.get("first_chunk_latency_ms")
    if first_chunk_ms is None:
        return finished_at
    started = finished_at - timedelta(milliseconds=float(row.get("duration_ms") or 0))
    return started + timedelta(milliseconds=float(first_chunk_ms))


def _turn_report(turn: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "transcript_id": turn["transcript_id"],
        "outcome": turn["outcome"],
        "tools": turn["tools"],
    }
    for stage, (start_key, end_key) in _STAGE_BOUNDS.items():
        start, end = turn.get(start_key), turn.get(end_key)
        report[stage] = round((end - start).total_seconds(), 3) if start and end else None
    return report
