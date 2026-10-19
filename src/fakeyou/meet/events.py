from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol, get_args

from fakeyou.foundation.taxonomy import TerminalReason
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.meet.meet import (
    CallObservation,
    CallState,
    JoinResult,
    LeaveResult,
)

PublicPhase = Literal[
    "created", "joining", "lobby", "admitted", "active", "leaving", "ended", "failed"
]
TERMINAL_PUBLIC_PHASES: frozenset[str] = frozenset({"ended", "failed"})
EVENT_VERSION = 1

_JOIN_PHASES: Mapping[str, PublicPhase] = {
    "admitted": "admitted",
    "denied": "failed",
    "blocked": "failed",
    "timeout": "failed",
    "aborted": "failed",
    "failed": "failed",
}
_LEAVE_PHASES: Mapping[str, PublicPhase] = {"left": "ended", "failed": "failed"}
_CALL_STATES: frozenset[str] = frozenset(get_args(CallState))
_PLAYBACK_EVENT_TYPES: Mapping[str, str | None] = {
    "playback_started": "twin.speech_started",
    "playback_finished": "twin.speech_finished",
    "playback_failed": None,
    "playback_dropped": None,
    "mute_failed": None,
}


class PlaybackLike(Protocol):
    @property
    def kind(self) -> str: ...


def _lookup[V](table: Mapping[str, V], value: str, domain: str) -> V:
    if value not in table:
        raise ValueError(f"{domain} value {value!r} has no public mapping")
    return table[value]


def phase_for_join(result: JoinResult) -> PublicPhase:
    return _lookup(_JOIN_PHASES, result.status, "join status")


def phase_for_call(obs: CallObservation, *, current: str) -> str:
    if obs.state not in _CALL_STATES:
        raise ValueError(f"call state value {obs.state!r} has no public mapping")
    if obs.state == "in_call":
        if current in ("joining", "lobby"):
            return "admitted"
        if current in ("admitted", "active"):
            return "active"
        return current
    if obs.state == "lobby":
        return "lobby" if current in ("created", "joining", "lobby") else current
    if obs.state == "removed":
        return "failed"
    if obs.state == "ended":
        return "ended"
    return current


def phase_for_leave(result: LeaveResult) -> PublicPhase:
    return _lookup(_LEAVE_PHASES, result.status, "leave status")


def phase_changed_event(
    meeting_id: str, *, phase: str, at: str, terminal_reason: str | None = None
) -> dict[str, object]:
    if phase not in get_args(PublicPhase):
        raise ValueError(f"phase value {phase!r} is outside the public enum")
    event: dict[str, object] = _base("phase_changed", meeting_id, at)
    event["phase"] = phase
    if phase in TERMINAL_PUBLIC_PHASES:
        if terminal_reason is None:
            raise ValueError(f"terminal phase {phase!r} requires terminal_reason")
        if terminal_reason not in get_args(TerminalReason):
            raise ValueError(f"terminal_reason {terminal_reason!r} is outside the domain")
        event["terminal_reason"] = terminal_reason
    elif terminal_reason is not None:
        raise ValueError("terminal_reason is only valid on terminal phases")
    return event


def transcript_final_event(
    meeting_id: str, event: TranscriptEvent, *, at: str
) -> dict[str, object]:
    if not event.finalized:
        raise ValueError("transcript.final requires a finalized transcript event")
    payload = _base("transcript.final", meeting_id, at)
    payload["id"] = event.id
    payload["participant_id"] = event.participant_id
    payload["text"] = event.text
    payload["visual_refs"] = list(event.visual_refs)
    payload["voice_clusters"] = list(event.voice_clusters)
    return payload


def speech_event(meeting_id: str, playback: PlaybackLike, *, at: str) -> dict[str, object] | None:
    event_type = _lookup(_PLAYBACK_EVENT_TYPES, playback.kind, "playback kind")
    if event_type is None:
        return None
    return _base(event_type, meeting_id, at)


def fault_event(meeting_id: str, *, at: str, source: str, detail: str) -> dict[str, object]:
    payload = _base("fault", meeting_id, at)
    payload["source"] = source
    payload["detail"] = detail
    return payload


def _base(event_type: str, meeting_id: str, at: str) -> dict[str, object]:
    return {"v": EVENT_VERSION, "type": event_type, "at": at, "meeting_id": meeting_id}
