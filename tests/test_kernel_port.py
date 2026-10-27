from fakeyou.audio.playback import PlaybackEvent
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.identity.identity import SpeakerRow
from fakeyou.orchestration.kernel_port import KernelPort
from fakeyou.perception.meet_protocol import MeetUser
from fakeyou.perception.roster import MeetRoster
from fakeyou.session.world import TWIN, Ref


def port() -> KernelPort:
    return KernelPort.for_meeting("meeting-9")


def row(uuid: str, device: str | None, *, enrolled: str | None = None) -> SpeakerRow:
    return SpeakerRow(
        uuid=uuid,
        label="Speaker A",
        device=device,
        status="confirmed",
        confidence=1.0,
        margin=1.0,
        slot=None,
        csrcs=(),
        track="",
        level=0.0,
        history=(),
        verified_enrollment=enrolled,
    )


def final(event_id: str, text: str) -> TranscriptEvent:
    return TranscriptEvent(
        id=event_id,
        timestamp="2026-10-27T00:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id="voice:a",
        speaker_evidence_id=None,
    )


def joined(kernel: KernelPort) -> None:
    roster = MeetRoster()
    roster.apply_users(
        [
            MeetUser(device_id="dev-twin", name="Rani", present=True, current_user=True),
            MeetUser(device_id="dev-ayu", name="Ayu", present=True),
        ]
    )
    roster.mark_synced()
    kernel.call_state("in_call")
    kernel.roster(roster)


def test_a_registry_bound_voice_makes_the_room_sole() -> None:
    kernel = port()
    joined(kernel)

    kernel.transcript(final("t1", "can you hear me?"), row("uuid-a", "dev-ayu"))
    kernel.drain()

    fact = kernel.presence()
    assert fact.sole_human_responder is True
    assert kernel.state.dialogue.lines[-1].speaker == Ref("track", "uuid-a")


def test_an_unbound_voice_keeps_the_room_open() -> None:
    kernel = port()
    joined(kernel)

    kernel.transcript(final("t1", "can you hear me?"), row("uuid-x", None))
    kernel.drain()

    assert kernel.presence().sole_human_responder is False
    assert kernel.presence().unresolved == 1


def test_the_same_final_is_only_reduced_once() -> None:
    kernel = port()
    joined(kernel)
    event = final("t1", "hello")

    kernel.transcript(event, None)
    kernel.transcript(event, None)
    kernel.drain()

    assert [line.text for line in kernel.state.dialogue.lines] == ["hello"]


def test_a_reconnect_starts_a_new_browser_epoch() -> None:
    kernel = port()
    joined(kernel)
    kernel.call_state("in_call")
    kernel.call_state("lobby")
    kernel.call_state("in_call", new_epoch=True)
    kernel.drain()

    assert kernel.state.world.browser_epoch == 2
    assert kernel.state.world.endpoints == {}


def test_a_clip_dropped_while_playing_is_an_interrupted_delivery() -> None:
    kernel = port()
    kernel.playback(PlaybackEvent("playback_started", "First."))
    kernel.playback(PlaybackEvent("playback_finished", "First."))
    kernel.playback(PlaybackEvent("playback_dropped", "Queued.", "interrupted"))
    kernel.playback(PlaybackEvent("playback_started", "Second"))
    kernel.playback(PlaybackEvent("playback_dropped", "Second", "interrupted"))
    kernel.playback(PlaybackEvent("playback_started", "Hmm.", cue=True))
    kernel.drain()

    spoken = [
        (line.text, line.interrupted)
        for line in kernel.state.dialogue.lines
        if line.speaker == TWIN
    ]
    assert spoken == [("First.", False), ("Second", True)]


def test_repeated_health_and_dom_readings_are_not_new_events() -> None:
    kernel = port()
    kernel.protocol_health(available=True)
    kernel.protocol_health(available=True)
    kernel.dom_remote(1)
    kernel.dom_remote(1)
    kernel.dom_remote(None)
    kernel.drain()

    assert kernel.state.seq == 2


def test_a_stale_protocol_reaches_presence() -> None:
    kernel = port()
    joined(kernel)
    kernel.protocol_health(available=False)
    kernel.drain()

    assert kernel.presence().status == "stale"


def test_own_and_backlog_chat_are_kept_as_dialogue() -> None:
    kernel = port()
    kernel.chat(message_id="m1", device_id="dev-twin", text="sent", own=True, backlog=False)
    kernel.chat(message_id="m0", device_id="dev-ayu", text="old", own=False, backlog=True)
    kernel.drain()

    lines = kernel.state.dialogue.lines
    assert (lines[0].speaker, lines[1].backlog) == (TWIN, True)
