from datetime import UTC, datetime

import pytest

from fakeyou.conversation.state_speech import locale_of, render
from fakeyou.session.commands import CommandOutcome
from fakeyou.session.contracts import (
    CallStateObserved,
    Endpoint,
    Observation,
    RosterObserved,
    ScreenObserved,
    SessionEvent,
    SourceHealthObserved,
    TranscriptObserved,
)
from fakeyou.session.facts import (
    Attendance,
    OwnAction,
    ParticipantCount,
    Presenter,
    Proposition,
    ScreenTitle,
    Unknown,
    state_fact,
    still_valid,
)
from fakeyou.session.world import SessionState, reduce

NOW = datetime(2026, 10, 27, tzinfo=UTC)
TWIN = Endpoint(device_id="dev-twin", display_name="Rani", present=True, current_user=True)
AYU = Endpoint(device_id="dev-ayu", display_name="Ayu", present=True)
BUDI = Endpoint(device_id="dev-budi", display_name="Budi", present=True)
IN_CALL = CallStateObserved(state="in_call", browser_epoch=1)


def replay(*observations: Observation, state: SessionState | None = None) -> SessionState:
    current = state or SessionState()
    for observation in observations:
        current = reduce(
            current,
            SessionEvent(
                event_id=f"e{current.seq}",
                session_id="meeting-9",
                attempt_id="attempt-1",
                source="meet_protocol",
                received_at=NOW,
                observation=observation,
            ),
        )
    return current


def test_a_complete_room_is_an_exact_count_and_a_full_name_list() -> None:
    state = replay(IN_CALL, RosterObserved(endpoints=(TWIN, AYU, BUDI), synced=True))

    assert state_fact("participant_count", state).proposition == ParticipantCount(2, 2)
    assert state_fact("attendance", state).proposition == Attendance(
        ("Ayu", "Budi"), 0, complete=True
    )


def test_an_unmapped_voice_turns_the_count_into_a_range() -> None:
    state = replay(
        IN_CALL,
        RosterObserved(endpoints=(TWIN,), synced=True),
        TranscriptObserved(transcript_id="t1", text="hi", final=True, track_id="voice-x"),
    )

    count = state_fact("participant_count", state).proposition
    attendance = state_fact("attendance", state).proposition

    assert count == ParticipantCount(1, None)
    assert attendance == Attendance((), 1, complete=False)


def test_attendance_is_a_claim_from_the_meeting_namespace_only() -> None:
    fact = state_fact("attendance", replay(IN_CALL, RosterObserved(endpoints=(TWIN, AYU))))

    assert (fact.namespace, fact.modality) == ("meeting_live", "participant_claimed")


def test_a_stale_protocol_answers_unknown_instead_of_empty() -> None:
    state = replay(
        IN_CALL,
        RosterObserved(endpoints=(TWIN, AYU), synced=True),
        SourceHealthObserved(source="meet_protocol", status="stale"),
    )

    assert state_fact("attendance", state).proposition == Unknown("attendance", "stale_source")
    assert state_fact("presenter", state).proposition == Unknown("presenter", "stale_source")


def test_a_screen_change_before_speech_invalidates_the_fact() -> None:
    sharing = replay(
        IN_CALL,
        RosterObserved(endpoints=(TWIN, AYU), synced=True),
        ScreenObserved(presenter_device_id="dev-ayu", title="Q4 roadmap"),
    )
    fact = state_fact("screen_title", sharing)
    changed = replay(ScreenObserved(presenter_device_id="dev-ayu", title="Budget"), state=sharing)

    assert fact.proposition == ScreenTitle("Q4 roadmap", sharing=True)
    assert still_valid(fact, sharing) is True
    assert still_valid(fact, changed) is False
    assert state_fact("presenter", changed).proposition == Presenter("Ayu", presenting=True)


def test_own_action_status_comes_from_the_latest_command() -> None:
    state = replay(IN_CALL)
    actions = (
        CommandOutcome("c1", "mute", "effect_observed"),
        CommandOutcome("c2", "react", "unknown"),
    )

    assert state_fact("own_action_status", state, actions).proposition == OwnAction(
        "react", "unknown"
    )
    assert state_fact("own_action_status", state).proposition == Unknown(
        "own_action_status", "no_record"
    )


@pytest.mark.parametrize(
    ("proposition", "english"),
    [
        (
            Attendance(("Ayu", "Budi", "Citra"), 0, complete=True),
            "I can see Ayu, Budi, and Citra here.",
        ),
        (
            Attendance(("Ayu",), 1, complete=False),
            "I can see Ayu and 1 more I can't put a name to.",
        ),
        (
            ParticipantCount(1, None),
            "There is at least one person besides me, but I can't confirm the exact number.",
        ),
        (
            ParticipantCount(3, None),
            "There are at least 3 people besides me, but I can't confirm the exact number.",
        ),
        (ParticipantCount(1, 1), "Besides me, there is one person here."),
        (Presenter(None, presenting=True), "Someone is presenting, but I can't tell who."),
        (
            OwnAction("react", "effect_unobserved"),
            "I sent a reaction, but the meeting didn't confirm it.",
        ),
        (OwnAction("raise_hand", "failed_after_boundary"), "My hand raise didn't go through."),
        (
            Unknown("participant_count", "stale_source"),
            "I can't confirm how many people are here right now.",
        ),
    ],
)
def test_state_speech_is_rendered_without_a_model(proposition: Proposition, english: str) -> None:
    assert render(proposition, "en") == english


def test_every_locale_renders_every_state() -> None:
    propositions = (
        Attendance(("Ayu",), 0, complete=True),
        ParticipantCount(0, 0),
        Presenter("Ayu", presenting=True),
        ScreenTitle(None, sharing=True),
        OwnAction("leave", "dispatching"),
        Unknown("screen_title", "not_in_call"),
    )

    for locale in ("en", "id"):
        assert all(render(proposition, locale) for proposition in propositions)


def test_only_languages_with_a_pack_get_state_speech() -> None:
    assert (locale_of("id-ID"), locale_of("en"), locale_of("ja")) == ("id", "en", None)
