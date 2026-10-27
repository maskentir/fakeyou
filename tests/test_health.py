from datetime import UTC, datetime

from fakeyou.session.contracts import (
    CallStateObserved,
    Endpoint,
    Observation,
    RosterObserved,
    SessionEvent,
    SourceHealthObserved,
    TranscriptObserved,
    VoiceTrackObserved,
)
from fakeyou.session.health import modes, side_effects_allowed
from fakeyou.session.identity import presence
from fakeyou.session.world import SessionState, reduce

NOW = datetime(2026, 10, 27, tzinfo=UTC)
IN_CALL = CallStateObserved(state="in_call", browser_epoch=1)
TWIN = Endpoint(device_id="dev-twin", display_name="Rani", present=True, current_user=True)
AYU = Endpoint(device_id="dev-ayu", display_name="Ayu", present=True)


def replay(*observations: Observation) -> SessionState:
    state = SessionState()
    for index, observation in enumerate(observations):
        state = reduce(
            state,
            SessionEvent(
                event_id=f"e{index}",
                session_id="meeting-9",
                attempt_id="attempt-1",
                source="meet_protocol",
                received_at=NOW,
                observation=observation,
            ),
        )
    return state


def test_a_healthy_call_has_no_degraded_mode() -> None:
    assert modes(replay(IN_CALL)) == frozenset()
    assert side_effects_allowed(frozenset()) is True


def test_outside_the_call_the_browser_is_unknown_and_side_effects_stop() -> None:
    active = modes(replay(IN_CALL, CallStateObserved(state="lobby", browser_epoch=1)))

    assert active == frozenset({"browser_unknown"})
    assert side_effects_allowed(active) is False


def test_each_unhealthy_source_names_its_mode() -> None:
    active = modes(
        replay(
            IN_CALL,
            SourceHealthObserved(source="meet_protocol", status="stale"),
            SourceHealthObserved(source="model", status="down"),
            SourceHealthObserved(source="memory", status="down"),
            SourceHealthObserved(source="playback", status="down"),
            SourceHealthObserved(source="speaker_registry", status="stale"),
        )
    )

    assert active == frozenset(
        {
            "perception_degraded",
            "model_degraded",
            "memory_degraded",
            "voice_degraded",
            "identity_degraded",
        }
    )
    assert side_effects_allowed(active) is False


def test_a_recovered_source_clears_its_mode() -> None:
    active = modes(
        replay(
            IN_CALL,
            SourceHealthObserved(source="memory", status="down"),
            SourceHealthObserved(source="memory", status="healthy"),
        )
    )

    assert active == frozenset()


def test_degraded_identity_turns_off_the_sole_responder() -> None:
    healthy = replay(
        IN_CALL,
        RosterObserved(endpoints=(TWIN, AYU), synced=True),
        VoiceTrackObserved(track_id="voice-a", device_id="dev-ayu"),
        TranscriptObserved(transcript_id="t1", text="hi", final=True, track_id="voice-a"),
    )
    degraded = reduce(
        healthy,
        SessionEvent(
            event_id="e-registry",
            session_id="meeting-9",
            attempt_id="attempt-1",
            source="speaker_registry",
            received_at=NOW,
            observation=SourceHealthObserved(source="speaker_registry", status="down"),
        ),
    )

    assert presence(healthy).sole_human_responder is True
    assert presence(degraded).sole_human_responder is False
