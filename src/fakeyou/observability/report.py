import json
import math
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fakeyou.observability.journal import JOURNAL_BACKUP_COUNT, journal_path

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
_MEET_ACTION_EVENTS = frozenset(
    {
        "meet_action_requested",
        "meet_action_started",
        "meet_action_observed",
        "meet_action_finished",
        "meet_action_failed",
        "meet_action_rejected",
    }
)
_MEET_ACTION_TERMINAL = {
    "meet_action_finished": "finished",
    "meet_action_failed": "failed",
    "meet_action_rejected": "rejected",
}
VISUAL_BUDGET_P95 = 12.0
VISUAL_BUDGET_MAX = 30.0
VISUAL_STABILITY_MIN = 0.95


SessionState = Literal["fresh", "unfinished", "finished"]


def describe_session(journal_dir: Path, meeting_id: str) -> SessionState:
    rows = read_journal(journal_path(journal_dir, meeting_id))
    if not rows:
        return "fresh"
    if any(row.get("event") == "session_finished" for row in rows):
        return "finished"
    return "unfinished"


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
    meet_actions: dict[str, dict[str, Any]] = {}
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
    order_inversions = 0
    drain_gaps = 0
    tap_slot_rescues = 0
    tap_slot_drops = 0
    turn_interims: list[float] = []
    turn_delays: list[float] = []
    capture_engines = Counter[str]()
    starts = 0
    outcomes = Counter[str]()
    reasons = Counter[str]()
    diff_classes = Counter[str]()
    norm_dist_buckets = Counter[str]()
    overlaps: list[float] = []
    visual_frames = 0
    visual_captioned = 0
    visual_cost_usd = 0.0
    visual_capture_ms: list[float] = []
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
            speaker_reason = row.get("speaker_reason")
            if isinstance(speaker_id, str) and speaker_id != "unknown":
                attributed += 1
            elif isinstance(speaker_reason, str):
                unknown_reasons[speaker_reason] += 1
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
            if row.get("source") == "llm" or _is_prefetch(row):
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
            elif _is_prefetch(row):
                request_id = row.get("request_id")
                if isinstance(request_id, str):
                    pending.pop(request_id, None)
                turn = _transcript_turn(turns, row, excluded)
                if turn is not None:
                    turn["recall_count"] += 1
                    _add_duration(turn, "recall_ms", row.get("duration_ms"), excluded)
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
        elif event in _MEET_ACTION_EVENTS:
            _record_meet_action(meet_actions, row, excluded)
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
        elif event == "order_inversion_detected":
            order_inversions += 1
        elif event == "drain_gap_detected":
            drain_gaps += 1
        elif event == "tap_slot_rescued":
            tap_slot_rescues += 1
        elif event == "tap_slot_dropped":
            tap_slot_drops += 1
        elif event == "stt_turn_recorded":
            interims = row.get("interims")
            if isinstance(interims, bool) or not isinstance(interims, (int, float)):
                continue
            turn_interims.append(float(interims))
            delay = row.get("first_interim_to_final_ms")
            if isinstance(delay, bool) or not isinstance(delay, (int, float)):
                continue
            turn_delays.append(float(delay))
        elif event == "capture_polled":
            slots = row.get("slots")
            if not isinstance(slots, dict):
                continue
            for slot in slots.values():
                engine = slot.get("engine") if isinstance(slot, dict) else None
                if isinstance(engine, str) and engine:
                    capture_engines[engine] += 1
        elif event == "visual_captured":
            visual_frames += 1
            duration = row.get("duration_ms")
            if (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and math.isfinite(duration)
                and duration >= 0
            ):
                visual_capture_ms.append(float(duration))
        elif event == "visual_captioned":
            visual_captioned += 1
            cost = row.get("cost_usd")
            if (
                isinstance(cost, (int, float))
                and not isinstance(cost, bool)
                and math.isfinite(cost)
                and cost >= 0
            ):
                visual_cost_usd += float(cost)
        elif event == "pregeneration_started":
            starts += 1
        elif event == "pregeneration_finished":
            outcome = str(row.get("outcome"))
            outcomes[outcome] += 1
            if outcome == "discarded":
                reasons[str(row.get("reason"))] += 1
            if "diff_class" in row:
                diff_classes[str(row.get("diff_class"))] += 1
            if "norm_dist_bucket" in row:
                norm_dist_buckets[str(row.get("norm_dist_bucket"))] += 1
            if outcome == "adopted":
                duration = row.get("duration_ms")
                if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                    overlaps.append(float(duration) / 1000)
    for transcript_id in pending.values():
        if isinstance(transcript_id, str) and transcript_id in turns:
            turns[transcript_id]["incomplete"] += 1
        elif isinstance(transcript_id, str):
            excluded["unknown_transcript"] += 1
        else:
            excluded["missing_transcript"] += 1
    turn_reports = [_turn_report(turn, excluded) for turn in turns.values()]
    if result is None or result.get("exit_code") is None:
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
        "meet_actions": _meet_actions_report(meet_actions),
        "interruptions": {
            "triggers": dict(barge_in_triggers),
            "speech_to_stop_seconds": summarize(interruption_seconds),
        },
        "stt_quality": {
            "order_inversions": order_inversions,
            "drain_gaps": drain_gaps,
            "tap_slot_rescues": tap_slot_rescues,
            "tap_slot_drops": tap_slot_drops,
            "turn_interims": summarize(turn_interims),
            "first_interim_to_final_ms": summarize(turn_delays),
            "capture_engines": dict(capture_engines),
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
        "pregeneration": {
            "starts": starts,
            "outcomes": dict(outcomes),
            "reasons": dict(reasons),
            "diff_classes": dict(diff_classes),
            "norm_dist_buckets": dict(norm_dist_buckets),
            "adopted_overlap_seconds": summarize(overlaps),
        },
        "visual": {
            "frames": visual_frames,
            "captioned": visual_captioned,
            "cost_usd": round(visual_cost_usd, 6),
            "p95_ms": summarize(visual_capture_ms)["p95"],
        },
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


def _is_prefetch(row: dict[str, Any]) -> bool:
    return row.get("source") == "memory" and row.get("operation") == "prefetch"


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


def _record_meet_action(
    meet: dict[str, dict[str, Any]], row: dict[str, Any], excluded: Counter[str]
) -> None:
    action_id = row.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        excluded["missing_action_id"] += 1
        return
    entry = meet.get(action_id)
    if entry is None:
        entry = {
            "action_id": action_id,
            "action": None,
            "outcome": None,
            "reason": None,
            "observed": False,
            "attempts": 1,
            "duration_ms": None,
        }
        meet[action_id] = entry
    if entry["outcome"] is not None:
        excluded["duplicate"] += 1
        return
    action = row.get("action")
    if entry["action"] is None and isinstance(action, str) and action:
        entry["action"] = action
    for key in ("attempt", "attempts"):
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(value) and value >= 1 and value > entry["attempts"]:
            entry["attempts"] = int(value)
    event = row.get("event")
    if event == "meet_action_observed":
        entry["observed"] = True
        return
    outcome = _MEET_ACTION_TERMINAL.get(event) if isinstance(event, str) else None
    if outcome is None:
        return
    entry["outcome"] = outcome
    reason = row.get("reason")
    entry["reason"] = reason if isinstance(reason, str) else None
    duration = row.get("duration_ms")
    if duration is not None:
        kind = _duration_kind(duration)
        if kind == "nonfinite":
            excluded["nonfinite_duration"] += 1
        elif kind == "invalid":
            excluded["invalid_duration"] += 1
        else:
            entry["duration_ms"] = duration
    if outcome == "finished" and row.get("observed") and not entry["observed"]:
        excluded["unobserved_finish"] += 1


def _meet_actions_report(meet: dict[str, dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, Counter[str]] = {}
    rejections = Counter[str]()
    durations: list[float] = []
    actions: list[dict[str, Any]] = []
    for entry in meet.values():
        action = entry["action"] if isinstance(entry["action"], str) else "unknown"
        outcome = entry["outcome"] if isinstance(entry["outcome"], str) else "pending"
        counts.setdefault(action, Counter())[outcome] += 1
        if outcome == "rejected" and isinstance(entry["reason"], str):
            rejections[entry["reason"]] += 1
        duration = entry["duration_ms"]
        if duration is not None:
            durations.append(float(duration) / 1000)
        actions.append(
            {
                "action_id": entry["action_id"],
                "action": action,
                "outcome": outcome,
                "reason": entry["reason"],
                "observed": entry["observed"],
                "attempts": entry["attempts"],
                "duration_ms": duration,
            }
        )
    return {
        "counts": {name: dict(outcomes) for name, outcomes in counts.items()},
        "rejections": dict(rejections),
        "duration_seconds": summarize(durations),
        "actions": actions,
    }


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
