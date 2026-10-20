import math
import statistics
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fakeyou.config.identifiers import SessionId
from fakeyou.foundation.jsonl import read_jsonl
from fakeyou.foundation.numbers import finite_float, real_number
from fakeyou.foundation.timeparse import parse_iso_datetime
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


SessionState = Literal["fresh", "unfinished", "finished"]


def describe_session(journal_dir: Path, meeting_id: SessionId) -> SessionState:
    rows = read_journal(journal_path(journal_dir, meeting_id))
    if not rows:
        return "fresh"
    if any(row.get("event") == "session_finished" for row in rows):
        return "finished"
    return "unfinished"


def read_journal(path: Path) -> list[dict[str, Any]]:
    rows, _ = _read_journal_counted(path)
    return rows


def read_journal_with_corrupt(path: Path) -> tuple[list[dict[str, Any]], int]:
    return _read_journal_counted(path)


def _read_journal_counted(path: Path) -> tuple[list[dict[str, Any]], int]:
    files = [Path(f"{path}.{index}") for index in range(JOURNAL_BACKUP_COUNT, 0, -1)] + [path]
    rows: list[dict[str, Any]] = []
    corrupt = 0
    for file in files:
        read = read_jsonl(file)
        rows.extend(read.rows)
        corrupt += read.skipped
    return rows, corrupt


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


@dataclass(slots=True)
class _ReportState:
    turns: dict[str, dict[str, Any]] = field(default_factory=dict)
    speech_stopped_at: datetime | None = None
    speech_started_at: datetime | None = None
    speak_started_at: datetime | None = None
    interruption_seconds: list[float] = field(default_factory=list)
    barge_in_triggers: Counter[str] = field(default_factory=Counter)
    active: dict[str, Any] | None = None
    replying: dict[str, Any] | None = None
    synthesis_turns: dict[str, str | None] = field(default_factory=dict)
    pending: dict[str, Any] = field(default_factory=dict)
    excluded: Counter[str] = field(default_factory=Counter)
    replies: Counter[str] = field(default_factory=Counter)
    tools: dict[str, Counter[str]] = field(default_factory=dict)
    meet_actions: dict[str, dict[str, Any]] = field(default_factory=dict)
    episodes: list[dict[str, Any]] = field(default_factory=list)
    faults: Counter[tuple[str, str]] = field(default_factory=Counter)
    result: dict[str, Any] | None = None
    milestones: dict[str, dict[str, Any]] = field(default_factory=dict)
    cancel_requested: int = 0
    cancel_completed: int = 0
    ingest_runs: list[tuple[bool | None, bool]] = field(default_factory=list)
    ingest_fault_pending: bool = False
    utterances: int = 0
    attributed: int = 0
    unknown_reasons: Counter[str] = field(default_factory=Counter)
    policy_actions: Counter[str] = field(default_factory=Counter)
    policy_reasons: Counter[str] = field(default_factory=Counter)
    observer_methods: Counter[str] = field(default_factory=Counter)
    order_inversions: int = 0
    drain_gaps: int = 0
    tap_slot_rescues: int = 0
    tap_slot_drops: int = 0
    turn_interims: list[float] = field(default_factory=list)
    turn_delays: list[float] = field(default_factory=list)
    capture_engines: Counter[str] = field(default_factory=Counter)
    starts: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    diff_classes: Counter[str] = field(default_factory=Counter)
    norm_dist_buckets: Counter[str] = field(default_factory=Counter)
    overlaps: list[float] = field(default_factory=list)
    perception: Counter[str] = field(default_factory=Counter)
    reading_ms: list[float] = field(default_factory=list)


def _parse_row_at(row: dict[str, Any]) -> datetime | None:
    at = row.get("at")
    if not isinstance(at, str):
        return None
    return parse_iso_datetime(at)


def _apply_speech_event(state: _ReportState, row: dict[str, Any], at: datetime | None) -> None:
    event = row.get("event")
    if event == "speech_stopped":
        state.speech_stopped_at = at
    elif event == "speech_started":
        state.speech_started_at = at
    else:
        state.barge_in_triggers[str(row.get("trigger"))] += 1


def _apply_transcript_finalized(
    state: _ReportState, row: dict[str, Any], at: datetime | None
) -> None:
    state.utterances += 1
    speaker_id = row.get("participant_id")
    speaker_reason = row.get("speaker_reason")
    if isinstance(speaker_id, str) and speaker_id != "unknown":
        state.attributed += 1
    elif isinstance(speaker_reason, str):
        state.unknown_reasons[speaker_reason] += 1
    elif row.get("speaker_evidence_id") is None:
        state.unknown_reasons["no_evidence"] += 1
    else:
        state.unknown_reasons["evidence_unmatched"] += 1
    transcript_id = str(row.get("transcript_id"))
    state.turns[transcript_id] = {
        "transcript_id": transcript_id,
        "speech_stopped_at": state.speech_stopped_at,
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
    state.speech_stopped_at = None


def _apply_agent_event(state: _ReportState, row: dict[str, Any], at: datetime | None) -> None:
    event = row.get("event")
    if event == "agent_started":
        state.active = _turn_for(state.turns, row)
        if state.active is not None:
            state.active["agent_at"] = at
    elif event == "agent_finished":
        state.replying = _turn_for(state.turns, row)
        if state.replying is not None:
            state.replying["reply_at"] = at
            state.replying["outcome"] = "silent"
    elif event == "agent_cancel_requested":
        state.cancel_requested += 1
    else:
        if event == "agent_cancelled":
            state.cancel_completed += 1
        turn = _turn_for(state.turns, row)
        if turn is not None:
            turn["outcome"] = "cancelled" if event == "agent_cancelled" else "failed"


def _apply_request_started(state: _ReportState, row: dict[str, Any], _at: datetime | None) -> None:
    if row.get("source") == "llm" or _is_prefetch(row):
        request_id = row.get("request_id")
        if isinstance(request_id, str):
            state.pending[request_id] = row.get("transcript_id")


def _apply_request_done(state: _ReportState, row: dict[str, Any], _at: datetime | None) -> None:
    if row.get("source") == "llm":
        request_id = row.get("request_id")
        if isinstance(request_id, str):
            state.pending.pop(request_id, None)
        turn = _transcript_turn(state.turns, row, state.excluded)
        if turn is not None:
            turn["llm_count"] += 1
            _add_duration(turn, "llm_ms", row.get("duration_ms"), state.excluded)
    elif _is_prefetch(row):
        request_id = row.get("request_id")
        if isinstance(request_id, str):
            state.pending.pop(request_id, None)
        turn = _transcript_turn(state.turns, row, state.excluded)
        if turn is not None:
            turn["recall_count"] += 1
            _add_duration(turn, "recall_ms", row.get("duration_ms"), state.excluded)


def _apply_tool_event(state: _ReportState, row: dict[str, Any], _at: datetime | None) -> None:
    if row.get("event") == "tool_started":
        request_id = row.get("request_id")
        if isinstance(request_id, str):
            state.pending[request_id] = row.get("transcript_id")
        return
    name = str(row.get("tool"))
    outcome = "finished" if row.get("event") == "tool_finished" else "failed"
    state.tools.setdefault(name, Counter())[outcome] += 1
    request_id = row.get("request_id")
    if isinstance(request_id, str):
        state.pending.pop(request_id, None)
    tool_transcript = row.get("transcript_id")
    if isinstance(tool_transcript, str):
        turn = state.turns.get(tool_transcript)
        if turn is None:
            state.excluded["unknown_transcript"] += 1
        else:
            turn["tools"].append(name)
            if name == "recall_meeting_context":
                turn["recall_count"] += 1
                _add_duration(turn, "recall_ms", row.get("duration_ms"), state.excluded)
    else:
        state.excluded["missing_transcript"] += 1
        if state.active is not None:
            state.active["tools"].append(name)


def _apply_answer_emitted(state: _ReportState, row: dict[str, Any], at: datetime | None) -> None:
    turn = _transcript_turn(state.turns, row, state.excluded)
    if turn is None:
        return
    turn["streamed"] = True
    if row.get("event") == "answer_sentence_emitted":
        if at is not None and turn.get("first_sentence_at") is None:
            turn["first_sentence_at"] = at
        turn["sentence_count"] += 1
    elif at is not None and turn.get("first_text_at") is None:
        turn["first_text_at"] = at


def _playback_drop_delay(state: _ReportState, row: dict[str, Any], at: datetime | None) -> None:
    if (
        row.get("detail") == "interrupted"
        and at is not None
        and state.speech_started_at is not None
        and state.speak_started_at is not None
        and state.speech_started_at >= state.speak_started_at
    ):
        state.interruption_seconds.append((at - state.speech_started_at).total_seconds())


def _apply_playback_event(state: _ReportState, row: dict[str, Any], at: datetime | None) -> None:
    event = row.get("event")
    if event == "playback_started":
        state.speak_started_at = at
        if state.replying is not None:
            state.replying.setdefault("speak_at", at)
    elif event == "playback_finished":
        state.replies["spoken"] += 1
        state.replies["trailing_questions"] += row.get("ends_with_question") is True
        if state.replying is not None:
            state.replying["outcome"] = "spoken"
        state.replying = None
    else:
        state.replies["interrupted"] += 1
        _playback_drop_delay(state, row, at)
        if state.replying is not None:
            state.replying["outcome"] = "interrupted"
        state.replying = None


def _link_synthesis_turn(state: _ReportState, row: dict[str, Any]) -> None:
    request_id = row.get("request_id")
    if not isinstance(request_id, str):
        return
    linked = row.get("transcript_id")
    if isinstance(linked, str) and linked in state.turns:
        if state.synthesis_turns.get(request_id, linked) != linked:
            state.synthesis_turns[request_id] = None
        else:
            state.synthesis_turns[request_id] = linked
    else:
        if isinstance(linked, str):
            state.excluded["unknown_transcript"] += 1
        if request_id in state.synthesis_turns:
            state.synthesis_turns[request_id] = None
        else:
            state.synthesis_turns[request_id] = (
                state.replying["transcript_id"] if state.replying is not None else None
            )


def _apply_synthesis_done(state: _ReportState, row: dict[str, Any], at: datetime | None) -> None:
    if not isinstance(row.get("request_id"), str):
        if state.replying is not None and "speak_at" in state.replying:
            pcm_at = _first_pcm_at(row, at, state.excluded)
            if pcm_at is not None:
                state.replying.setdefault("first_pcm_at", pcm_at)
    else:
        turn = _synthesis_turn(state.turns, state.synthesis_turns, row, state.excluded)
        if turn is not None:
            pcm_at = _first_pcm_at(row, at, state.excluded)
            if pcm_at is not None:
                turn.setdefault("first_pcm_at", pcm_at)


def _apply_synthesis_event(state: _ReportState, row: dict[str, Any], at: datetime | None) -> None:
    event = row.get("event")
    if event == "synthesis_started":
        _link_synthesis_turn(state, row)
    elif event == "synthesis_pcm_emitted":
        turn = _synthesis_turn(state.turns, state.synthesis_turns, row, state.excluded)
        if turn is not None and at is not None:
            turn.setdefault("first_pcm_at", at)
    else:
        _apply_synthesis_done(state, row, at)


def _apply_memory_event(state: _ReportState, row: dict[str, Any], _at: datetime | None) -> None:
    event = row.get("event")
    if event in ("memory_episode_ingested", "memory_episode_failed"):
        state.episodes.append(row)
    elif event == "memory_ingest_finished":
        raw_completed = row.get("completed")
        state.ingest_runs.append(
            (
                bool(raw_completed) if isinstance(raw_completed, bool) else None,
                state.ingest_fault_pending,
            )
        )
        state.ingest_fault_pending = False
    else:
        state.faults[(str(row.get("source")), str(row.get("kind")))] += 1
        if row.get("source") == "memory_ingest":
            state.ingest_fault_pending = True


def _apply_counter_event(state: _ReportState, row: dict[str, Any], _at: datetime | None) -> None:
    event = row.get("event")
    if event == "order_inversion_detected":
        state.order_inversions += 1
    elif event == "drain_gap_detected":
        state.drain_gaps += 1
    elif event == "tap_slot_rescued":
        state.tap_slot_rescues += 1
    elif event == "tap_slot_dropped":
        state.tap_slot_drops += 1
    else:
        state.starts += 1


def _apply_session_event(state: _ReportState, row: dict[str, Any], _at: datetime | None) -> None:
    event = row.get("event")
    if event == "session_finished":
        state.result = {key: row.get(key) for key in ("phase", "terminal_reason", "exit_code")}
    elif event == "policy_decided":
        state.policy_actions[str(row.get("action"))] += 1
        state.policy_reasons[str(row.get("reason"))] += 1
    else:
        state.observer_methods[str(row.get("method"))] += 1


def _apply_stt_turn(state: _ReportState, row: dict[str, Any], _at: datetime | None) -> None:
    interims = real_number(row.get("interims"))
    if interims is None:
        return
    state.turn_interims.append(interims)
    delay = real_number(row.get("first_interim_to_final_ms"))
    if delay is None:
        return
    state.turn_delays.append(delay)


def _apply_capture_polled(state: _ReportState, row: dict[str, Any], _at: datetime | None) -> None:
    slots = row.get("slots")
    if not isinstance(slots, dict):
        return
    for slot in slots.values():
        engine = slot.get("engine") if isinstance(slot, dict) else None
        if isinstance(engine, str) and engine:
            state.capture_engines[engine] += 1


def _apply_perception_event(state: _ReportState, row: dict[str, Any], _at: datetime | None) -> None:
    event = str(row.get("event"))
    if event == "budget_exhausted" and row.get("source") != "perception":
        return
    if event == "screen_captured":
        event = f"screen_{row.get('change')}"
    elif event == "meeting_view_emitted" and row.get("image_attached") is True:
        state.perception["view_with_image"] += 1
    elif event == "screen_reading_finished":
        duration = finite_float(row.get("duration_ms"))
        if duration is not None and duration >= 0:
            state.reading_ms.append(duration)
    state.perception[event] += 1


def _perception_section(state: _ReportState) -> dict[str, Any]:
    counts = state.perception
    return {
        "screens": {
            "new": counts["screen_new"],
            "revisited": counts["screen_revisit"],
            "merged": counts["screen_duplicate_detected"],
        },
        "readings": {
            "finished": counts["screen_reading_finished"],
            "failed": counts["screen_reading_failed"],
            "skipped": counts["screen_reading_skipped"],
            "p95_ms": summarize(state.reading_ms)["p95"],
        },
        "views": {
            "emitted": counts["meeting_view_emitted"],
            "with_image": counts["view_with_image"],
        },
        "chat_lines": counts["chat_line_observed"],
        "signals": counts["meeting_signal_observed"],
        "rejected_inputs": counts["perception_input_rejected"],
        "budget_exhausted": counts["budget_exhausted"],
        "interpreter_disabled": counts["perception_interpreter_disabled"],
    }


def _apply_pregeneration_finished(
    state: _ReportState, row: dict[str, Any], _at: datetime | None
) -> None:
    outcome = str(row.get("outcome"))
    state.outcomes[outcome] += 1
    if outcome == "discarded":
        state.reasons[str(row.get("reason"))] += 1
    if "diff_class" in row:
        state.diff_classes[str(row.get("diff_class"))] += 1
    if "norm_dist_bucket" in row:
        state.norm_dist_buckets[str(row.get("norm_dist_bucket"))] += 1
    if outcome == "adopted":
        duration = real_number(row.get("duration_ms"))
        if duration is not None:
            state.overlaps.append(duration / 1000)


def _apply_meet_action(state: _ReportState, row: dict[str, Any], _at: datetime | None) -> None:
    _record_meet_action(state.meet_actions, row, state.excluded)


_RowHandler = Callable[[_ReportState, dict[str, Any], datetime | None], None]

_ROW_HANDLERS: dict[str, _RowHandler] = {
    "speech_stopped": _apply_speech_event,
    "speech_started": _apply_speech_event,
    "barge_in_detected": _apply_speech_event,
    "transcript_finalized": _apply_transcript_finalized,
    "agent_started": _apply_agent_event,
    "agent_finished": _apply_agent_event,
    "agent_cancelled": _apply_agent_event,
    "agent_failed": _apply_agent_event,
    "agent_cancel_requested": _apply_agent_event,
    "request_started": _apply_request_started,
    "request_finished": _apply_request_done,
    "request_failed": _apply_request_done,
    "tool_started": _apply_tool_event,
    "tool_finished": _apply_tool_event,
    "tool_failed": _apply_tool_event,
    "answer_text_emitted": _apply_answer_emitted,
    "answer_sentence_emitted": _apply_answer_emitted,
    "playback_started": _apply_playback_event,
    "playback_finished": _apply_playback_event,
    "playback_dropped": _apply_playback_event,
    "synthesis_started": _apply_synthesis_event,
    "synthesis_pcm_emitted": _apply_synthesis_event,
    "synthesis_finished": _apply_synthesis_event,
    "memory_episode_ingested": _apply_memory_event,
    "memory_episode_failed": _apply_memory_event,
    "memory_ingest_finished": _apply_memory_event,
    "fault_recorded": _apply_memory_event,
    "session_finished": _apply_session_event,
    "policy_decided": _apply_session_event,
    "participant_observer_started": _apply_session_event,
    "order_inversion_detected": _apply_counter_event,
    "drain_gap_detected": _apply_counter_event,
    "tap_slot_rescued": _apply_counter_event,
    "tap_slot_dropped": _apply_counter_event,
    "stt_turn_recorded": _apply_stt_turn,
    "capture_polled": _apply_capture_polled,
    "screen_captured": _apply_perception_event,
    "screen_duplicate_detected": _apply_perception_event,
    "screen_reading_finished": _apply_perception_event,
    "screen_reading_failed": _apply_perception_event,
    "screen_reading_skipped": _apply_perception_event,
    "meeting_view_emitted": _apply_perception_event,
    "chat_line_observed": _apply_perception_event,
    "meeting_signal_observed": _apply_perception_event,
    "perception_input_rejected": _apply_perception_event,
    "budget_exhausted": _apply_perception_event,
    "perception_interpreter_disabled": _apply_perception_event,
    "pregeneration_started": _apply_counter_event,
    "pregeneration_finished": _apply_pregeneration_finished,
}
for _meet_event in _MEET_ACTION_EVENTS:
    _ROW_HANDLERS[_meet_event] = _apply_meet_action


def _close_pending(state: _ReportState) -> None:
    for transcript_id in state.pending.values():
        if isinstance(transcript_id, str) and transcript_id in state.turns:
            state.turns[transcript_id]["incomplete"] += 1
        elif isinstance(transcript_id, str):
            state.excluded["unknown_transcript"] += 1
        else:
            state.excluded["missing_transcript"] += 1


def _session_outcome(result: dict[str, Any] | None) -> str:
    if result is None or result.get("exit_code") is None:
        return "unknown"
    if result.get("exit_code") == 0:
        return "completed"
    return "failed"


def _ingestion_outcome(ingest_runs: list[tuple[bool | None, bool]]) -> str:
    if not ingest_runs:
        return "unknown"
    run_completed, run_faulted = ingest_runs[-1]
    if run_completed is None:
        return "unknown"
    if run_completed:
        return "completed"
    if run_faulted:
        return "failed"
    return "interrupted"


def _ingestion_section(episodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
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
    }


def _assemble_report(
    state: _ReportState, turn_reports: list[dict[str, Any]], *, corrupt_lines: int
) -> dict[str, Any]:
    return {
        "journal": {"corrupt_lines": corrupt_lines},
        "startup": _startup_report(state.milestones),
        "session_outcome": _session_outcome(state.result),
        "ingestion_outcome": _ingestion_outcome(state.ingest_runs),
        "cancellation": {
            "requested": state.cancel_requested,
            "completed": state.cancel_completed,
        },
        "participation": {
            "utterances_recorded": state.utterances,
            "attributed_utterances": state.attributed,
            "attribution_coverage": round(state.attributed / state.utterances, 3)
            if state.utterances
            else None,
            "unknown_reasons": dict(state.unknown_reasons),
            "answers": sum(1 for turn in turn_reports if turn["llm_count"] > 0),
            "policy": dict(state.policy_actions),
            "policy_reasons": dict(state.policy_reasons),
            "observer_methods": dict(state.observer_methods),
        },
        "turns": turn_reports,
        "latency": {
            stage: summarize([turn[stage] for turn in turn_reports if turn[stage] is not None])
            for stage in LATENCY_STAGES
        },
        "evidence": {"excluded": dict(state.excluded)},
        "replies": {
            key: state.replies[key] for key in ("spoken", "interrupted", "trailing_questions")
        },
        "tools": {name: dict(counts) for name, counts in state.tools.items()},
        "meet_actions": _meet_actions_report(state.meet_actions),
        "interruptions": {
            "triggers": dict(state.barge_in_triggers),
            "speech_to_stop_seconds": summarize(state.interruption_seconds),
        },
        "stt_quality": {
            "order_inversions": state.order_inversions,
            "drain_gaps": state.drain_gaps,
            "tap_slot_rescues": state.tap_slot_rescues,
            "tap_slot_drops": state.tap_slot_drops,
            "turn_interims": summarize(state.turn_interims),
            "first_interim_to_final_ms": summarize(state.turn_delays),
            "capture_engines": dict(state.capture_engines),
        },
        "ingestion": _ingestion_section(state.episodes),
        "faults": [
            {"source": source, "kind": kind, "count": count}
            for (source, kind), count in state.faults.items()
        ],
        "pregeneration": {
            "starts": state.starts,
            "outcomes": dict(state.outcomes),
            "reasons": dict(state.reasons),
            "diff_classes": dict(state.diff_classes),
            "norm_dist_buckets": dict(state.norm_dist_buckets),
            "adopted_overlap_seconds": summarize(state.overlaps),
        },
        "perception": _perception_section(state),
        "result": state.result,
    }


def build_session_report(rows: list[dict[str, Any]], *, corrupt_lines: int = 0) -> dict[str, Any]:
    state = _ReportState()
    for row in rows:
        event = row.get("event")
        at = _parse_row_at(row)
        if event in STARTUP_MILESTONES and row.get("source") == "runtime":
            state.milestones.setdefault(str(event), {**row, "at": at})
        handler = _ROW_HANDLERS.get(event) if event is not None else None
        if handler is not None:
            handler(state, row, at)
    _close_pending(state)
    turn_reports = [_turn_report(turn, state.excluded) for turn in state.turns.values()]
    return _assemble_report(state, turn_reports, corrupt_lines=corrupt_lines)


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


def _meet_entry(
    meet: dict[str, dict[str, Any]], row: dict[str, Any], excluded: Counter[str]
) -> dict[str, Any] | None:
    action_id = row.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        excluded["missing_action_id"] += 1
        return None
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
        return None
    return entry


def _merge_meet_attempts(entry: dict[str, Any], row: dict[str, Any]) -> None:
    for key in ("attempt", "attempts"):
        value = finite_float(row.get(key))
        if value is not None and value >= 1 and value > entry["attempts"]:
            entry["attempts"] = int(value)


def _finish_meet_entry(
    entry: dict[str, Any], outcome: str, row: dict[str, Any], excluded: Counter[str]
) -> None:
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


def _record_meet_action(
    meet: dict[str, dict[str, Any]], row: dict[str, Any], excluded: Counter[str]
) -> None:
    entry = _meet_entry(meet, row, excluded)
    if entry is None:
        return
    action = row.get("action")
    if entry["action"] is None and isinstance(action, str) and action:
        entry["action"] = action
    _merge_meet_attempts(entry, row)
    event = row.get("event")
    if event == "meet_action_observed":
        entry["observed"] = True
        return
    outcome = _MEET_ACTION_TERMINAL.get(event) if isinstance(event, str) else None
    if outcome is None:
        return
    _finish_meet_entry(entry, outcome, row, excluded)


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
    number = real_number(value)
    if number is None:
        return "invalid"
    if not math.isfinite(number):
        return "nonfinite"
    if number < 0:
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
