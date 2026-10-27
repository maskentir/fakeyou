from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from fakeyou.session.contracts import (
    AudioStreamObserved,
    CallStateObserved,
    ChatObserved,
    Endpoint,
    Observation,
    ParticipantCountObserved,
    RosterObserved,
    SessionEvent,
    SourceHealthObserved,
    TranscriptObserved,
    VoiceTrackObserved,
)
from fakeyou.session.identity import attribute, presence
from fakeyou.session.world import UNKNOWN, Ref, SessionState, reduce

NOW = datetime(2026, 10, 27, tzinfo=UTC)
TWIN = Endpoint(device_id="dev-twin", display_name="Rani", present=True, current_user=True)
AYU = Endpoint(device_id="dev-ayu", display_name="Ayu", present=True)
BUDI = Endpoint(device_id="dev-budi", display_name="Budi", present=True)
IN_CALL = CallStateObserved(state="in_call", browser_epoch=1)


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


def said(track: str, stream: str | None = None) -> TranscriptObserved:
    return TranscriptObserved(
        transcript_id=f"t-{track}", text="hello", final=True, track_id=track, stream_id=stream
    )


def test_a_synced_room_with_one_mapped_human_is_complete_and_sole() -> None:
    fact = presence(
        replay(
            IN_CALL,
            RosterObserved(endpoints=(TWIN, AYU), synced=True),
            AudioStreamObserved(device_id="dev-ayu", stream_id="s-ayu"),
            said("voice-a", "s-ayu"),
        )
    )

    assert fact.status == "complete"
    assert (fact.confirmed_min, fact.plausible_max) == (1, 1)
    assert fact.sole_human_responder is True


def test_a_sync_with_only_the_twin_and_an_unmapped_voice_is_not_sole() -> None:
    fact = presence(
        replay(IN_CALL, RosterObserved(endpoints=(TWIN,), synced=True), said("voice-a"))
    )

    assert fact.status == "incomplete"
    assert fact.unresolved == 1
    assert fact.confirmed_min == 1
    assert fact.plausible_max is None
    assert fact.sole_human_responder is False


def test_two_voices_on_one_endpoint_make_a_range_not_one_person() -> None:
    state = replay(
        IN_CALL,
        RosterObserved(endpoints=(TWIN, AYU), synced=True),
        AudioStreamObserved(device_id="dev-ayu", stream_id="s-ayu"),
        VoiceTrackObserved(track_id="voice-a", stream_id="s-ayu"),
        VoiceTrackObserved(track_id="voice-b", stream_id="s-ayu"),
        said("voice-a"),
        said("voice-b"),
    )
    fact = presence(state)

    assert fact.confirmed_min == 2
    assert fact.plausible_max is None
    assert fact.shared_endpoints == 1
    assert fact.sole_human_responder is False
    assert attribute(state, Ref("track", "voice-a")).status == "probable"


def test_one_person_on_two_devices_stays_two_participants() -> None:
    laptop = Endpoint(device_id="dev-ayu-laptop", display_name="Ayu", present=True)
    phone = Endpoint(device_id="dev-ayu-phone", display_name="Ayu", present=True)
    fact = presence(replay(IN_CALL, RosterObserved(endpoints=(TWIN, laptop, phone), synced=True)))

    assert [participant.device_id for participant in fact.participants] == [
        "dev-ayu-laptop",
        "dev-ayu-phone",
    ]
    assert all(participant.claim_conflict for participant in fact.participants)
    assert fact.confirmed_min == 2
    assert fact.sole_human_responder is False


def test_a_stale_protocol_makes_presence_unknown_rather_than_empty() -> None:
    fact = presence(
        replay(
            IN_CALL,
            RosterObserved(endpoints=(TWIN, AYU), synced=True),
            SourceHealthObserved(source="meet_protocol", status="stale"),
        )
    )

    assert fact.status == "stale"
    assert fact.confirmed_min == 0
    assert fact.plausible_max is None
    assert fact.sole_human_responder is False


def test_a_dom_count_that_disagrees_with_the_roster_is_drift() -> None:
    fact = presence(
        replay(
            IN_CALL,
            RosterObserved(endpoints=(TWIN, AYU), synced=True),
            ParticipantCountObserved(remote=3),
        )
    )

    assert fact.drift is True
    assert fact.status == "incomplete"


def test_without_the_twin_endpoint_presence_cannot_be_complete() -> None:
    fact = presence(replay(IN_CALL, RosterObserved(endpoints=(AYU,), synced=True)))

    assert fact.twin_identified is False
    assert fact.status == "incomplete"


def test_chat_from_an_unknown_device_is_an_unresolved_participant() -> None:
    fact = presence(
        replay(
            IN_CALL,
            RosterObserved(endpoints=(TWIN, AYU), synced=True),
            ChatObserved(message_id="m1", device_id="dev-ghost", text="hi"),
        )
    )

    assert fact.unresolved == 1
    assert fact.sole_human_responder is False


def test_a_new_browser_epoch_needs_a_fresh_sync() -> None:
    fact = presence(
        replay(
            IN_CALL,
            RosterObserved(endpoints=(TWIN, AYU), synced=True),
            CallStateObserved(state="in_call", browser_epoch=2),
        )
    )

    assert fact.participants == ()
    assert fact.status == "incomplete"


def test_overlapping_speech_without_a_source_is_not_attributed() -> None:
    state = replay(IN_CALL, RosterObserved(endpoints=(TWIN, AYU), synced=True))

    assert attribute(state, UNKNOWN).status == "unknown"


def test_an_enrolled_voice_keeps_its_verified_person() -> None:
    state = replay(
        IN_CALL,
        RosterObserved(endpoints=(TWIN, AYU), synced=True),
        AudioStreamObserved(device_id="dev-ayu", stream_id="s-ayu"),
        VoiceTrackObserved(track_id="voice-a", stream_id="s-ayu", enrolled_person="owner"),
    )

    attribution = attribute(state, Ref("track", "voice-a"))

    assert attribution.device_id == "dev-ayu"
    assert attribution.display_claim == "Ayu"
    assert attribution.enrolled_person == "owner"
    assert attribution.status == "confirmed"


ids = st.sampled_from(["a", "b", "c"])
noise = st.one_of(
    st.builds(
        RosterObserved,
        endpoints=st.tuples(
            st.builds(
                Endpoint,
                device_id=ids,
                display_name=ids,
                present=st.booleans(),
                current_user=st.booleans(),
            )
        ),
        synced=st.booleans(),
    ),
    st.builds(ParticipantCountObserved, remote=st.integers(0, 4)),
    st.builds(AudioStreamObserved, device_id=ids, stream_id=ids),
    st.builds(VoiceTrackObserved, track_id=ids, stream_id=st.none() | ids),
    st.builds(
        TranscriptObserved,
        transcript_id=ids,
        text=ids,
        final=st.sampled_from([True]),
        track_id=st.none() | ids,
        stream_id=st.none() | ids,
    ),
    st.builds(ChatObserved, message_id=ids, device_id=ids, text=ids),
    st.builds(
        SourceHealthObserved,
        source=st.just("meet_protocol"),
        status=st.sampled_from(["healthy", "stale"]),
    ),
    st.builds(CallStateObserved, state=st.just("in_call"), browser_epoch=st.integers(1, 2)),
)


@given(st.lists(noise, max_size=30))
def test_an_exact_count_only_comes_from_complete_presence(items: list[Observation]) -> None:
    fact = presence(replay(IN_CALL, *items))

    if fact.plausible_max is not None:
        assert fact.complete
        assert fact.unresolved == 0
        assert fact.shared_endpoints == 0
    if fact.sole_human_responder:
        assert len(fact.participants) == 1
        assert fact.confirmed_min == 1


def test_a_registry_device_binding_resolves_a_voice_without_a_stream() -> None:
    state = replay(
        IN_CALL,
        RosterObserved(endpoints=(TWIN, AYU), synced=True),
        VoiceTrackObserved(track_id="voice-a", device_id="dev-ayu"),
        said("voice-a"),
    )

    assert presence(state).sole_human_responder is True
    assert attribute(state, Ref("track", "voice-a")).device_id == "dev-ayu"
