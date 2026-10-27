from dataclasses import astuple
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fakeyou.session.contracts import (
    AudioStreamObserved,
    CallStateObserved,
    ChatObserved,
    Endpoint,
    Observation,
    OwnerControlObserved,
    ParticipantCountObserved,
    PlaybackObserved,
    RosterObserved,
    ScreenObserved,
    SessionEvent,
    SourceHealthObserved,
    TranscriptObserved,
    VoiceTrackObserved,
)
from fakeyou.session.kernel import Mailbox, SessionKernel
from fakeyou.session.world import TWIN, Limits, Ref, SessionState, reduce

NOW = datetime(2026, 10, 27, tzinfo=UTC)
TWIN_ENDPOINT = Endpoint(device_id="dev-twin", display_name="Rani", present=True, current_user=True)
AYU = Endpoint(device_id="dev-ayu", display_name="Ayu", present=True)
LIMITS = Limits(dialogue_lines=5, interims=3)

ids = st.sampled_from(["a", "b", "c", "d"])
endpoints = st.builds(Endpoint, device_id=ids, display_name=ids, present=st.booleans())
observations = st.one_of(
    st.builds(RosterObserved, endpoints=st.tuples(endpoints, endpoints), synced=st.booleans()),
    st.builds(ParticipantCountObserved, remote=st.integers(0, 5)),
    st.builds(AudioStreamObserved, device_id=ids, stream_id=ids),
    st.builds(VoiceTrackObserved, track_id=ids, stream_id=st.none() | ids),
    st.builds(
        TranscriptObserved,
        transcript_id=ids,
        text=ids,
        final=st.booleans(),
        stream_id=st.none() | ids,
        track_id=st.none() | ids,
    ),
    st.builds(ChatObserved, message_id=ids, device_id=ids, text=ids, own=st.booleans()),
    st.builds(ScreenObserved, title=st.none() | ids),
    st.builds(
        CallStateObserved,
        state=st.sampled_from(["in_call", "in_call", "in_call", "left"]),
        browser_epoch=st.integers(0, 2),
    ),
    st.builds(
        SourceHealthObserved,
        source=st.sampled_from(["meet_protocol", "stt", "vision"]),
        status=st.sampled_from(["healthy", "stale", "down"]),
    ),
    st.builds(
        PlaybackObserved,
        utterance_id=ids,
        sentence_index=st.integers(0, 2),
        phase=st.sampled_from(["started", "finished", "interrupted", "failed"]),
        text=ids,
    ),
    st.builds(OwnerControlObserved, command=st.sampled_from(["stop_playback", "pause", "resume"])),
)


def event(
    event_id: str, observation: Observation, *, source_seq: int | None = None
) -> SessionEvent:
    return SessionEvent(
        event_id=event_id,
        session_id="meeting-9",
        attempt_id="attempt-1",
        source="meet_protocol",
        received_at=NOW,
        observation=observation,
        source_seq=source_seq,
    )


def numbered(items: list[Observation]) -> list[SessionEvent]:
    return [event(f"e{index}", observation) for index, observation in enumerate(items)]


def kernel() -> SessionKernel:
    return SessionKernel(session_id="meeting-9", attempt_id="attempt-1", limits=LIMITS)


def replay(events: list[SessionEvent]) -> SessionState:
    state = SessionState()
    for item in events:
        state = reduce(state, item, limits=LIMITS)
    return state


@given(st.lists(observations, max_size=40))
def test_the_reducer_is_deterministic(items: list[Observation]) -> None:
    events = numbered(items)

    assert replay(events) == replay(events)


@given(st.lists(observations, max_size=40))
def test_duplicate_events_do_not_change_the_state(items: list[Observation]) -> None:
    once = kernel()
    twice = kernel()
    for item in numbered(items):
        once.accept(item)
        twice.accept(item)
        twice.accept(item)

    assert twice.state == once.state
    assert twice.state.seq == len(items)


@given(st.lists(observations, max_size=60))
def test_the_projections_stay_bounded(items: list[Observation]) -> None:
    state = replay(numbered(items))

    assert len(state.dialogue.lines) <= LIMITS.dialogue_lines
    assert len(state.dialogue.interims) <= LIMITS.interims


@given(st.lists(observations, max_size=40))
def test_revisions_never_go_backwards(items: list[Observation]) -> None:
    state = SessionState()
    for item in numbered(items):
        updated = reduce(state, item, limits=LIMITS)
        assert all(
            after >= before
            for before, after in zip(
                astuple(state.revisions), astuple(updated.revisions), strict=True
            )
        )
        state = updated


@given(st.lists(observations, max_size=20))
def test_nothing_but_playback_changes_after_the_call_ends(items: list[Observation]) -> None:
    ended = reduce(SessionState(), event("end", CallStateObserved(state="left", browser_epoch=0)))
    state = ended
    for item in numbered(items):
        state = reduce(state, item, limits=LIMITS)

    assert state.terminal
    assert state.world == ended.world
    assert state.agenda == ended.agenda


def test_an_endpoint_missing_from_a_sync_is_not_absent() -> None:
    state = replay(
        [
            event("e1", RosterObserved(endpoints=(TWIN_ENDPOINT, AYU), synced=True)),
            event("e2", RosterObserved(endpoints=(TWIN_ENDPOINT,), synced=True)),
        ]
    )

    assert state.world.endpoints["dev-ayu"].presence == "present"


def test_an_explicit_departure_is_absent() -> None:
    gone = AYU.model_copy(update={"present": False})
    state = replay(
        [
            event("e1", RosterObserved(endpoints=(TWIN_ENDPOINT, AYU), synced=True)),
            event("e2", RosterObserved(endpoints=(gone,))),
        ]
    )

    assert state.world.endpoints["dev-ayu"].presence == "absent"


def test_a_new_browser_epoch_expires_devices_and_stream_bindings() -> None:
    state = replay(
        [
            event("e1", CallStateObserved(state="in_call", browser_epoch=1)),
            event("e2", RosterObserved(endpoints=(TWIN_ENDPOINT, AYU), synced=True)),
            event("e3", AudioStreamObserved(device_id="dev-ayu", stream_id="s1")),
            event("e4", VoiceTrackObserved(track_id="voice-a", stream_id="s1")),
            event("e5", CallStateObserved(state="in_call", browser_epoch=2)),
        ]
    )

    assert state.world.endpoints == {}
    assert state.world.streams == {}
    assert state.world.roster_synced is False
    assert state.world.tracks["voice-a"].stream_id is None


def test_chat_does_not_invalidate_presence_but_a_roster_change_does() -> None:
    joined = replay([event("e1", RosterObserved(endpoints=(TWIN_ENDPOINT,), synced=True))])
    chatted = reduce(
        joined, event("e2", ChatObserved(message_id="m1", device_id="dev-twin", text="hi"))
    )
    rostered = reduce(chatted, event("e3", RosterObserved(endpoints=(AYU,))))

    assert chatted.revisions.presence == joined.revisions.presence
    assert chatted.revisions.dialogue == joined.revisions.dialogue + 1
    assert rostered.revisions.presence == joined.revisions.presence + 1


def test_an_interim_holds_the_floor_until_its_final() -> None:
    interim = TranscriptObserved(transcript_id="t1", text="so", final=False, track_id="voice-a")
    final = interim.model_copy(update={"text": "so what now", "final": True})

    holding = replay([event("e1", interim)])
    released = reduce(holding, event("e2", final))

    assert holding.dialogue.floor_holder == Ref("track", "voice-a")
    assert released.dialogue.floor_holder is None
    assert released.dialogue.interims == {}
    assert released.dialogue.lines[-1].text == "so what now"
    assert released.world.heard[Ref("track", "voice-a")] == released.seq


def test_delivered_and_interrupted_twin_speech_is_kept_for_repeat() -> None:
    state = replay(
        [
            event(
                "e1",
                PlaybackObserved(utterance_id="u1", sentence_index=0, phase="started", text="One."),
            ),
            event(
                "e2",
                PlaybackObserved(
                    utterance_id="u1", sentence_index=0, phase="finished", text="One."
                ),
            ),
            event(
                "e3",
                PlaybackObserved(
                    utterance_id="u1", sentence_index=1, phase="interrupted", text="Two"
                ),
            ),
            event(
                "e4",
                PlaybackObserved(
                    utterance_id="u1", sentence_index=2, phase="dropped", text="Three."
                ),
            ),
        ]
    )

    spoken = [
        (line.text, line.interrupted) for line in state.dialogue.lines if line.speaker == TWIN
    ]
    assert spoken == [("One.", False), ("Two", True)]
    assert state.dialogue.twin_playing == frozenset()


def test_owner_pause_and_resume_move_the_agenda() -> None:
    paused = replay([event("e1", OwnerControlObserved(command="pause"))])
    resumed = reduce(paused, event("e2", OwnerControlObserved(command="resume")))

    assert paused.agenda.paused is True
    assert resumed.agenda.paused is False
    assert resumed.revisions.agenda == 2


def test_an_event_behind_its_source_sequence_is_counted_late() -> None:
    session = kernel()
    session.accept(event("e1", ParticipantCountObserved(remote=2), source_seq=5))
    state = session.accept(event("e2", ParticipantCountObserved(remote=3), source_seq=4))

    assert state.health.late_events == 1
    assert state.world.dom_remote == 3


def test_an_event_from_another_attempt_is_refused() -> None:
    other = event("e1", ParticipantCountObserved(remote=1)).model_copy(update={"attempt_id": "x"})

    with pytest.raises(ValueError, match="another session attempt"):
        kernel().accept(other)


def test_control_events_jump_the_mailbox_queue() -> None:
    mailbox = Mailbox(capacity=4)
    mailbox.offer(event("e1", ChatObserved(message_id="m1", device_id="d", text="hi")))
    mailbox.offer(event("e2", OwnerControlObserved(command="stop_playback")))

    first = mailbox.take_nowait()

    assert first is not None
    assert first.event_id == "e2"


def test_a_full_mailbox_drops_interims_but_never_semantic_events() -> None:
    mailbox = Mailbox(capacity=2)
    interim = TranscriptObserved(transcript_id="t", text="uh", final=False)
    mailbox.offer(event("e1", interim))
    mailbox.offer(event("e2", ChatObserved(message_id="m1", device_id="d", text="a")))
    mailbox.offer(event("e3", ChatObserved(message_id="m2", device_id="d", text="b")))
    mailbox.offer(event("e4", ChatObserved(message_id="m3", device_id="d", text="c")))
    mailbox.offer(event("e5", interim))

    taken = [item.event_id for item in iter(mailbox.take_nowait, None)]

    assert taken == ["e2", "e3", "e4"]
    assert mailbox.dropped == 2
    assert mailbox.saturated is False


def test_a_saturated_mailbox_is_reported_as_degraded_health() -> None:
    session = kernel()
    mailbox = Mailbox(capacity=1)
    mailbox.offer(event("e1", ChatObserved(message_id="m1", device_id="d", text="a")))
    mailbox.offer(event("e2", ChatObserved(message_id="m2", device_id="d", text="b")))

    session.note_mailbox(mailbox)

    assert session.state.health.saturated is True
    assert session.state.revisions.health == 1
