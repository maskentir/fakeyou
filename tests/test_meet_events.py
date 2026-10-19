from __future__ import annotations

from typing import cast, get_args

import pytest
from pydantic import BaseModel

from fakeyou.audio.playback import PlaybackEvent, PlaybackEventKind
from fakeyou.config.settings import Settings
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.meet.meet import CallObservation, JoinResult, LeaveResult

AT = "2026-10-16T00:00:00Z"


def _final_transcript(*, finalized: bool = True) -> TranscriptEvent:
    return TranscriptEvent(
        id="utt-1",
        timestamp=AT,
        source="muse",
        text="hello everyone",
        finalized=finalized,
        participant_id="speaker-a",
        speaker_evidence_id=None,
        visual_refs=("frame-1",),
        voice_clusters=("cluster-a",),
    )


def test_join_status_maps_to_public_phase() -> None:
    from fakeyou.meet.events import phase_for_join

    assert phase_for_join(JoinResult("admitted", "")) == "admitted"
    assert phase_for_join(JoinResult("denied", "host denied")) == "failed"
    assert phase_for_join(JoinResult("timeout", "lobby timeout")) == "failed"


def test_unknown_call_state_keeps_current_phase() -> None:
    from fakeyou.meet.events import phase_for_call

    obs = CallObservation("unknown", "no marker matched")
    assert phase_for_call(obs, current="active") == "active"
    assert phase_for_call(CallObservation("removed", ""), current="active") == "failed"
    assert phase_for_call(CallObservation("ended", ""), current="active") == "ended"


def test_leave_result_maps_to_terminal_phase() -> None:
    from fakeyou.meet.events import phase_for_leave

    assert phase_for_leave(LeaveResult("left", "")) == "ended"
    assert phase_for_leave(LeaveResult("failed", "tab closed")) == "failed"


def test_terminal_phase_event_requires_reason() -> None:
    from fakeyou.meet.events import phase_changed_event

    with pytest.raises(ValueError, match="terminal_reason"):
        phase_changed_event("m-1", phase="ended", at=AT)
    event = phase_changed_event("m-1", phase="ended", at=AT, terminal_reason="left_by_owner")
    assert event == {
        "v": 1,
        "type": "phase_changed",
        "at": AT,
        "meeting_id": "m-1",
        "phase": "ended",
        "terminal_reason": "left_by_owner",
    }


def test_phase_event_rejects_values_outside_public_enum() -> None:
    from fakeyou.meet.events import phase_changed_event

    with pytest.raises(ValueError, match="joining_retry"):
        phase_changed_event("m-1", phase="joining_retry", at=AT)


def test_speech_event_maps_only_started_and_finished() -> None:
    from fakeyou.meet.events import speech_event

    started = speech_event("m-1", PlaybackEvent("playback_started", "hello"), at=AT)
    assert started is not None
    assert started["type"] == "twin.speech_started"
    finished = speech_event("m-1", PlaybackEvent("playback_finished"), at=AT)
    assert finished is not None
    assert finished["type"] == "twin.speech_finished"
    assert speech_event("m-1", PlaybackEvent("playback_dropped"), at=AT) is None
    assert speech_event("m-1", PlaybackEvent("mute_failed"), at=AT) is None


def test_unknown_playback_kind_fails_loudly() -> None:
    from fakeyou.meet.events import speech_event

    with pytest.raises(ValueError, match="playback_exploded"):
        speech_event("m-1", PlaybackEvent(cast(PlaybackEventKind, "playback_exploded")), at=AT)


def test_transcript_final_event_carries_only_contract_fields() -> None:
    from fakeyou.meet.events import transcript_final_event

    event = transcript_final_event("m-1", _final_transcript(), at=AT)
    assert event == {
        "v": 1,
        "type": "transcript.final",
        "at": AT,
        "meeting_id": "m-1",
        "id": "utt-1",
        "participant_id": "speaker-a",
        "text": "hello everyone",
        "visual_refs": ["frame-1"],
        "voice_clusters": ["cluster-a"],
    }


def test_transcript_final_event_rejects_interim_update() -> None:
    from fakeyou.meet.events import transcript_final_event

    with pytest.raises(ValueError, match="finalized"):
        transcript_final_event("m-1", _final_transcript(finalized=False), at=AT)


def test_fault_event_names_its_source() -> None:
    from fakeyou.meet.events import fault_event

    event = fault_event("m-1", at=AT, source="stt", detail="no_transcripts")
    assert event == {
        "v": 1,
        "type": "fault",
        "at": AT,
        "meeting_id": "m-1",
        "source": "stt",
        "detail": "no_transcripts",
    }


def test_public_phase_mirror_matches_conversation_enum() -> None:
    from typing import get_args

    from fakeyou.conversation.state import PublicPhase
    from fakeyou.meet.events import PublicPhase as MeetPublicPhase

    assert set(get_args(MeetPublicPhase)) == set(get_args(PublicPhase))


def _section_models(annotation: object) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    models: list[type[BaseModel]] = []
    for member in get_args(annotation):
        models.extend(_section_models(member))
    return models


def _setting_env_keys(model: type[BaseModel], prefix: str = "FAKEYOU_") -> set[str]:
    keys: set[str] = set()
    for name, field in model.model_fields.items():
        members = _section_models(field.annotation)
        if members:
            for member in members:
                keys |= _setting_env_keys(member, f"{prefix}{name}__")
        else:
            keys.add(f"{prefix}{name}")
    return keys


RETIRED_FLAGS = frozenset(
    {
        "conversation.per_stream_enabled",
        "conversation.voice_labels_enabled",
        "conversation.meet_actions_enabled",
        "identity.voiceprint_enabled",
        "conversation.participant_policy_enabled",
        "audio.persist_enabled",
        "visual.enabled",
        "stt.idle_reconnect_seconds",
    }
)


def test_retired_flags_are_decided_and_voiceprint_is_still_off() -> None:
    live = {name.lower() for name in _setting_env_keys(Settings)}
    leaf_names = {name.rsplit("__", 1)[-1] for name in live}

    assert RETIRED_FLAGS <= {
        "conversation.per_stream_enabled",
        "conversation.voice_labels_enabled",
        "conversation.meet_actions_enabled",
        "identity.voiceprint_enabled",
        "conversation.participant_policy_enabled",
        "audio.persist_enabled",
        "visual.enabled",
        "stt.idle_reconnect_seconds",
    }
    assert Settings().identity.voiceprint_enabled is False
    assert Settings().visual.enabled is False
    assert Settings().conversation.sentence_streaming is True
    assert leaf_names


def test_no_flag_field_survived_its_retirement() -> None:
    live = {name.lower() for name in _setting_env_keys(Settings)}

    for retired in ("attribution_probe_enabled", "pre_generate_enabled"):
        assert retired not in live
