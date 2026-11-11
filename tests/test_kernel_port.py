from typing import Any, cast

import pytest

from fakeyou.audio.playback import PlaybackEvent
from fakeyou.conversation.turn_judge import JudgeLine, JudgeWindow, TurnJudgement
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.identity.identity import SpeakerRow
from fakeyou.orchestration.kernel_port import HealthReportingJudge, HeardTurnEnd, KernelPort
from fakeyou.perception.meet_protocol import MeetUser
from fakeyou.perception.roster import MeetRoster
from fakeyou.session.contracts import ChatObserved, Endpoint, RosterObserved
from fakeyou.session.world import TWIN, Ref
from tests._persistence_fakes import RecordingJournal


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


def test_an_early_transcript_is_attributed_without_starting_a_turn() -> None:
    kernel = port()
    joined(kernel)
    signals: list[object] = []

    class Collecting:
        def accept(self, signal: object) -> None:
            signals.append(signal)

        async def idle(self) -> None:
            return None

    kernel.on_turn(cast(Any, Collecting()))
    kernel.early(final("early-1", "can you hear me?"), row("uuid-a", "dev-ayu"))
    kernel.drain()

    actor = kernel.actor("early-1")
    assert (actor.kind, actor.device_id) == ("participant", "dev-ayu")
    assert kernel.state.dialogue.lines[-1].speaker == Ref("track", "uuid-a")
    assert signals == []


def test_a_turn_end_score_is_offered_to_turn_consumers() -> None:
    kernel = port()
    joined(kernel)
    signals: list[object] = []

    class Collecting:
        def accept(self, signal: object) -> None:
            signals.append(signal)

        async def idle(self) -> None:
            return None

    kernel.on_turn(cast(Any, Collecting()))
    kernel.turn_end(0.9)
    kernel.drain()

    assert signals == [HeardTurnEnd(0.9)]


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
    kernel.playback(PlaybackEvent("playback_audio_started", "First."))
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
    kernel.chat(
        ChatObserved(message_id="m1", device_id="dev-twin", text="sent", own=True, backlog=False)
    )
    kernel.chat(
        ChatObserved(message_id="m0", device_id="dev-ayu", text="old", own=False, backlog=True)
    )
    kernel.drain()

    lines = kernel.state.dialogue.lines
    assert (lines[0].speaker, lines[1].backlog) == (TWIN, True)


class FailingJudge:
    async def judge(self, window: JudgeWindow) -> TurnJudgement:
        raise TimeoutError(window.latest.text)


class AnsweringJudge:
    async def judge(self, window: JudgeWindow) -> TurnJudgement:
        return TurnJudgement(
            heard=window.latest.text,
            finished=True,
            backchannel=False,
            addressee="twin",
            action="reply",
            confidence=0.9,
            owner_claim=False,
        )


WINDOW = JudgeWindow(earlier=(), latest=JudgeLine(speaker="ayu", text="Rani?"))


async def test_a_failing_judge_puts_the_session_in_model_degraded_mode() -> None:
    journal = RecordingJournal()
    kernel = KernelPort.for_meeting("meeting-9", journal=journal)
    joined(kernel)

    with pytest.raises(TimeoutError):
        await HealthReportingJudge(FailingJudge(), kernel).judge(WINDOW)
    kernel.drain()

    assert kernel.modes() == frozenset({"model_degraded"})
    assert kernel.side_effects_allowed() is False

    await HealthReportingJudge(AnsweringJudge(), kernel).judge(WINDOW)
    kernel.drain()

    assert kernel.modes() == frozenset()
    transitions = journal.events("session_mode_transitioned")
    assert [(row["entered"], row["left"]) for row in transitions][-2:] == [
        (["model_degraded"], []),
        ([], ["model_degraded"]),
    ]


def test_recall_lands_in_the_kernel_recall_set() -> None:
    kernel = port()
    joined(kernel)

    kernel.recall_observed(query="budget", outcome="found", fact_refs=("edge-1", "edge-2"))
    kernel.drain()

    assert kernel.state.recall.outcome == "found"
    assert kernel.state.recall.fact_refs == ("edge-1", "edge-2")
    assert kernel.state.recall.query == "budget"
    assert kernel.modes() == frozenset()


def test_finals_reach_the_recall_listener_with_the_dialogue_window() -> None:
    kernel = port()
    joined(kernel)
    heard: list[tuple[str, tuple[str, ...]]] = []
    kernel.on_final(lambda transcript_id, window: heard.append((transcript_id, window)))

    kernel.transcript(final("t1", "what about the budget?"), row("uuid-a", "dev-ayu"))
    kernel.transcript(final("t2", "and the one from yesterday?"), row("uuid-a", "dev-ayu"))
    kernel.drain()

    assert heard == [
        ("t1", ("what about the budget?",)),
        ("t2", ("what about the budget?", "and the one from yesterday?")),
    ]


def test_unavailable_memory_and_failed_playback_are_degraded_modes() -> None:
    kernel = port()
    joined(kernel)
    kernel.memory_outcome("unavailable")
    kernel.playback(PlaybackEvent("playback_started", "Hi."))
    kernel.playback(PlaybackEvent("playback_failed", "Hi."))
    kernel.drain()

    assert kernel.modes() == frozenset({"memory_degraded", "voice_degraded"})


def person(row: dict[str, Any], device_id: str) -> dict[str, Any]:
    return next(entry for entry in row["people"] if entry["id"] == device_id)


def test_the_roster_is_journaled_once_per_change_with_names_only() -> None:
    journal = RecordingJournal()
    kernel = KernelPort.for_meeting("meeting-9", journal=journal)
    joined(kernel)
    joined(kernel)

    rows = journal.events("roster_observed")
    assert len(rows) == 1
    assert rows[0]["people"] == [
        {"id": "dev-ayu", "name": "Ayu", "own": False, "parent": None, "present": True},
        {"id": "dev-twin", "name": "Rani", "own": True, "parent": None, "present": True},
    ]
    assert rows[0]["synced"] is True

    roster = MeetRoster()
    roster.apply_users(
        [
            MeetUser(device_id="dev-twin", name="Rani", present=True, current_user=True),
            MeetUser(device_id="dev-ayu", name="Ayu", present=True),
            MeetUser(device_id="dev-bimo", name="Bimo", present=True),
        ]
    )
    roster.mark_synced()
    kernel.roster(roster)

    rows = journal.events("roster_observed")
    assert len(rows) == 2
    assert person(rows[1], "dev-bimo")["name"] == "Bimo"


def test_a_departure_and_a_screen_are_journaled_for_replay() -> None:
    journal = RecordingJournal()
    kernel = KernelPort.for_meeting("meeting-9", journal=journal)
    joined(kernel)

    roster = MeetRoster()
    roster.apply_users(
        [
            MeetUser(device_id="dev-twin", name="Rani", present=True, current_user=True),
            MeetUser(device_id="dev-ayu", name="Ayu", present=False),
            MeetUser(
                device_id="dev-ayu-screen",
                name="Ayu",
                present=True,
                parent_device_id="dev-ayu",
            ),
        ]
    )
    kernel.roster(roster)

    rows = journal.events("roster_observed")
    assert len(rows) == 2
    assert rows[1]["synced"] is False
    assert person(rows[1], "dev-ayu")["present"] is False
    assert person(rows[1], "dev-ayu-screen")["parent"] == "dev-ayu"


def test_a_replayed_roster_makes_presence_named() -> None:
    kernel = port()
    kernel.call_state("in_call")
    kernel.observe(
        "meet_protocol",
        RosterObserved(
            endpoints=(
                Endpoint(
                    device_id="dev-twin", display_name="Rani", present=True, current_user=True
                ),
                Endpoint(device_id="dev-ayu", display_name="Ayu", present=True),
            ),
            synced=True,
        ),
    )
    kernel.drain()

    fact = kernel.presence()
    assert fact.twin_identified is True
    assert [person.display_claim for person in fact.participants] == ["Ayu"]


def test_a_replayed_departure_screen_and_second_twin_device_stay_out_of_presence() -> None:
    kernel = port()
    kernel.call_state("in_call")

    def observe(endpoints: tuple[Endpoint, ...], *, synced: bool) -> None:
        kernel.observe("meet_protocol", RosterObserved(endpoints=endpoints, synced=synced))
        kernel.drain()

    twin = Endpoint(device_id="dev-twin", display_name="Rani", present=True, current_user=True)
    twin_phone = Endpoint(
        device_id="dev-twin-phone", display_name="Rani", present=True, current_user=True
    )
    ayu = Endpoint(device_id="dev-ayu", display_name="Ayu", present=True)
    screen = Endpoint(
        device_id="dev-ayu-screen",
        display_name="Ayu",
        present=True,
        parent_device_id="dev-ayu",
    )
    observe((twin, twin_phone, ayu, screen), synced=False)

    fact = kernel.presence()
    assert fact.complete is False
    assert [person.display_claim for person in fact.participants] == ["Ayu"]

    observe((twin, twin_phone, ayu, screen), synced=True)
    assert kernel.presence().complete is True

    observe((ayu.model_copy(update={"present": False}),), synced=True)
    assert kernel.presence().participants == ()


def test_degraded_identity_attributes_nobody() -> None:
    kernel = port()
    joined(kernel)
    kernel.chat(
        ChatObserved(
            message_id="m1", device_id="dev-ayu", text="Rani, hand up", own=False, backlog=False
        )
    )
    kernel.source_health("speaker_registry", "down")
    kernel.drain()

    assert kernel.actor("m1").kind == "unattributed"
