import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from fakeyou.audio import _CaptureObserver
from fakeyou.browser import open_direct_page
from fakeyou.deaf import CaptureActivity
from fakeyou.ingestion import extraction_instructions, meeting_windows
from fakeyou.probe_replay import (
    LABEL_STABILITY_GATE,
    OVERLAP_UNKNOWN_GATE,
    replay_voice_labels,
    voice_fixture_from_probe,
)
from fakeyou.record import TranscriptEntry
from fakeyou.report import build_session_report
from fakeyou.runtime import run_meeting_session
from fakeyou.session import SessionConfig
from fakeyou.settings import DEFAULTS
from fakeyou.voice_activity import (
    MEET_AUDIO_SLOTS,
    VOICE_ACTIVITY_DRAIN_JS,
    SlotBinder,
    VoiceActivityLoop,
    VoiceActivityTracker,
    load_voice_activity_script,
    voice_label,
    voice_speaker_name,
)
from tests.fakes import (
    IN_CALL,
    LINK,
    FakeClock,
    FakePageSession,
    FakeSaverSession,
    RecordingJournal,
    meet_page,
)

FIXTURE = Path(__file__).parent / "fixtures" / "voice-labels-probe-attr-8.json"
CSRC_FIXTURE = Path(__file__).parent / "fixtures" / "voice-labels-csrc-live-3.json"


def batch(
    page_ms: float,
    levels: list[list[float]],
    participants: list[dict[str, str]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    roster = participants if participants is not None else [{"id": "d1", "name": "Rani"}]
    return {"instance": "i1", "page_ms": page_ms, "levels": levels, "participants": roster, **extra}


def tracker_at(clock: FakeClock) -> VoiceActivityTracker:
    return VoiceActivityTracker(twin_name="Twin", monotonic=clock.monotonic)


def speak(tracker: VoiceActivityTracker, clock: FakeClock, start: float, end: float) -> None:
    clock.now = start
    tracker.observe_vad(True)
    clock.now = end
    tracker.observe_vad(False)


def test_voice_labels_run_past_z() -> None:
    assert [voice_label(index) for index in (0, 1, 25, 26, 27)] == ["A", "B", "Z", "AA", "AB"]
    assert voice_speaker_name("speaker-ab") == "Speaker AB"
    assert voice_speaker_name("unknown") is None
    assert voice_speaker_name("speaker-") is None
    assert voice_speaker_name("Rani") is None


def test_binder_binds_lone_device_and_releases_only_its_slot() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    host = binder.resolve(3)
    assert not isinstance(host, str)
    assert (host.label, host.device) == ("A", "host")
    binder.update_roster(["host", "guest"])
    guest = binder.resolve(4)
    assert not isinstance(guest, str)
    assert (guest.label, guest.device) == ("B", "guest")
    binder.update_roster(["host"])
    still_host = binder.resolve(3)
    assert not isinstance(still_host, str)
    assert still_host.label == "A"
    binder.update_roster(["host", "returning"])
    returning = binder.resolve(4)
    assert not isinstance(returning, str)
    assert (returning.label, returning.device) == ("C", "returning")


def test_binder_departure_retires_oldest_unbound_slot() -> None:
    binder = SlotBinder()
    binder.update_roster(["a", "b", "c"])
    first = binder.resolve(3)
    second = binder.resolve(4)
    assert not isinstance(first, str) and not isinstance(second, str)
    assert first.device is None and second.device is None
    binder.update_roster(["a", "b"])
    replaced = binder.resolve(3)
    assert not isinstance(replaced, str)
    assert replaced.label == "C"
    kept = binder.resolve(4)
    assert not isinstance(kept, str)
    assert kept.label == "B"


def test_binder_refuses_more_slots_than_unbound_devices() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    assert not isinstance(binder.resolve(3), str)
    assert binder.resolve(4) == "slot_conflict"


def test_binder_refuses_labels_without_roster() -> None:
    binder = SlotBinder()
    assert binder.resolve(3) == "no_roster"


def test_binder_drops_tenures_while_contended() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    assert not isinstance(binder.resolve(3), str)
    binder.update_roster([f"d{index}" for index in range(MEET_AUDIO_SLOTS + 1)])
    binder.update_roster(["host"])
    fresh = binder.resolve(3)
    assert not isinstance(fresh, str)
    assert fresh.label == "B"


def test_binder_never_starts_a_label_while_twin_speaks() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    assert binder.resolve(3, allow_new=False) == "self_overlap"
    assert not isinstance(binder.resolve(3), str)
    known = binder.resolve(3, allow_new=False)
    assert not isinstance(known, str)
    assert known.label == "A"


def test_tracker_labels_dominant_slot_with_pipeline_lag() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.2, 11.2)
    clock.now = 11.4
    tracker.observe_batch(
        batch(1000.0 + 11_400.0, [[1000.0 + 10_050.0, 3, 0.3], [1000.0 + 10_100.0, 4, 0.05]]),
        sent_ms=11_400.0,
        received_ms=11_400.0,
    )
    attribution = tracker.attribute()
    assert attribution.participant_id == "speaker-a"
    assert attribution.reason == "dominant_slot"
    assert attribution.slot == 3
    assert attribution.bound is True
    assert attribution.evidence_id == "voice-1"
    assert attribution.journal_fields()["speaker_levels"] == {"3": 0.3, "4": 0.05}


def test_tracker_reports_overlap_as_unknown() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(
            11_000.0,
            [[10_300.0, 3, 0.2], [10_400.0, 4, 0.15]],
            [{"id": "d1", "name": "Rani"}, {"id": "d2", "name": "Budi"}],
        ),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    attribution = tracker.attribute()
    assert (attribution.participant_id, attribution.reason) == ("unknown", "overlap")


def test_tracker_needs_speech_fresh_taps_and_a_loud_source() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    assert tracker.attribute().reason == "no_speech"
    speak(tracker, clock, 10.0, 11.0)
    assert tracker.attribute().reason == "stale"
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        batch(13_000.0, [[12_500.0, 3, 0.004]]), sent_ms=13_000.0, received_ms=13_000.0
    )
    assert tracker.attribute().reason == "no_source"


def test_tracker_calls_partly_drained_silence_stale() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(10_300.0, [[10_000.0, 3, 0.004]]), sent_ms=10_300.0, received_ms=10_300.0
    )
    assert tracker.attribute().reason == "stale"


def test_extraction_instructions_explain_voice_labels_only_when_present() -> None:
    assert "anonymous voice labels" not in extraction_instructions("Rani Twin", ())
    assert "anonymous voice labels" in extraction_instructions("Rani Twin", (), voice_labels=True)


def test_tracker_only_counts_speech_since_previous_transcript() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    roster = [{"id": "d1", "name": "Rani"}, {"id": "d2", "name": "Budi"}]
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [[10_400.0, 3, 0.3]], roster), sent_ms=11_000.0, received_ms=11_000.0
    )
    assert tracker.attribute().slot == 3
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        batch(13_000.0, [[12_400.0, 4, 0.3]], roster), sent_ms=13_000.0, received_ms=13_000.0
    )
    second = tracker.attribute()
    assert (second.slot, second.participant_id) == (4, "speaker-b")


def test_tracker_splits_open_speech_between_transcripts() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    tracker.observe_vad(True)
    clock.now = 12.0
    tracker.observe_batch(
        batch(12_000.0, [[10_500.0, 3, 0.3]]), sent_ms=12_000.0, received_ms=12_000.0
    )
    assert tracker.attribute().slot == 3
    clock.now = 14.0
    tracker.observe_batch(batch(14_000.0, []), sent_ms=14_000.0, received_ms=14_000.0)
    assert tracker.attribute().reason == "no_source"


def test_tracker_maps_page_clock_with_lowest_round_trip() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(batch(3_100.0, []), sent_ms=11_090.0, received_ms=11_110.0)
    tracker.observe_batch(
        batch(6_000.0, [[2_300.0, 3, 0.3]]), sent_ms=12_000.0, received_ms=14_000.0
    )
    assert tracker.attribute().slot == 3


def test_tracker_ignores_twin_tile_and_local_slots() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(
            11_000.0,
            [[10_300.0, 0, 0.5], [10_300.0, 3, 0.2]],
            [{"id": "self", "name": " twin "}, {"id": "d1", "name": "Rani"}],
            local=[0],
        ),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    attribution = tracker.attribute()
    assert (attribution.slot, attribution.participant_id, attribution.bound) == (
        3,
        "speaker-a",
        True,
    )


def test_tracker_refuses_new_label_while_twin_plays() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    tracker.observe_self_speech(True)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_self_speech(False)
    tracker.observe_batch(
        batch(11_000.0, [[10_300.0, 3, 0.3]]), sent_ms=11_000.0, received_ms=11_000.0
    )
    assert tracker.attribute().reason == "self_overlap"


def test_tracker_restarts_labels_when_page_script_reloads() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [[10_300.0, 3, 0.3]]), sent_ms=11_000.0, received_ms=11_000.0
    )
    assert tracker.attribute().participant_id == "speaker-a"
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        {**batch(500.0, [[-200.0, 3, 0.3]]), "instance": "i2"},
        sent_ms=13_000.0,
        received_ms=13_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-b"


def test_tracker_waits_out_tile_flicker_before_release() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [[10_300.0, 3, 0.3]]), sent_ms=11_000.0, received_ms=11_000.0
    )
    assert tracker.attribute().participant_id == "speaker-a"
    clock.now = 11.5
    tracker.observe_batch(batch(11_500.0, [], []), sent_ms=11_500.0, received_ms=11_500.0)
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        batch(13_000.0, [[12_300.0, 3, 0.3]]), sent_ms=13_000.0, received_ms=13_000.0
    )
    assert tracker.attribute().participant_id == "speaker-a"


def test_tracker_keeps_roster_when_batch_has_no_participants() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    tracker.observe_batch(batch(10_000.0, []), sent_ms=10_000.0, received_ms=10_000.0)
    speak(tracker, clock, 20.0, 21.0)
    roster_less = {"instance": "i1", "page_ms": 21_000.0, "levels": [[20_300.0, 3, 0.3]]}
    tracker.observe_batch(roster_less, sent_ms=21_000.0, received_ms=21_000.0)
    assert tracker.attribute().participant_id == "speaker-a"


def test_tracker_skips_malformed_levels() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [["x", 3, 0.9], [10_300.0, None, 0.9], [10_300.0], [10_300.0, 3, 0.2]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    tracker.observe_batch({"page_ms": "late"}, sent_ms=11_000.0, received_ms=11_000.0)
    assert tracker.attribute().slot == 3


async def test_voice_activity_loop_feeds_tracker_and_survives_failures() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    page = meet_page([], evaluates=[batch(10_000.0, []), None, RuntimeError("gone")])
    loop = VoiceActivityLoop(page, tracker)
    assert await loop.poll() is True
    assert await loop.poll() is False
    assert await loop.poll() is False
    assert page.evaluate_calls == [VOICE_ACTIVITY_DRAIN_JS] * 3


async def test_capture_observer_reports_vad_edges_to_voice_tracker() -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection

    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    observer = _CaptureObserver(CaptureActivity(now=clock.monotonic), voice_activity=tracker)
    await observer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    clock.now = 11.0
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    tracker.observe_batch(
        batch(11_000.0, [[10_300.0, 3, 0.3]]), sent_ms=11_000.0, received_ms=11_000.0
    )
    assert tracker.attribute().slot == 3


def test_voice_labels_default_off() -> None:
    assert DEFAULTS.conversation.voice_labels_enabled is False


async def test_run_meeting_session_installs_voice_script_and_starts_drain() -> None:
    stop = asyncio.Event()
    clock = FakeClock()
    page = meet_page(
        [IN_CALL, IN_CALL, IN_CALL],
        on_content=lambda reads: stop.set() if reads >= 2 else None,
        evaluates=[batch(0.0, [])] * 8,
    )
    tracker = VoiceActivityTracker(twin_name="Twin", monotonic=clock.monotonic)
    config = SessionConfig(owner_id="o", twin_id="t", meeting_id="m", link=LINK, twin_name="Twin")
    journal = RecordingJournal("m")
    result = await run_meeting_session(
        config,
        stop=stop,
        open_page=FakePageSession(page).open,
        saver_factory=FakeSaverSession().factory,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        journal=journal,
        voice_activity=tracker,
    )
    assert result.phase == "ended"
    assert page.init_scripts == [load_voice_activity_script()]
    assert journal.events("voice_activity_started")


def test_ingestion_names_voice_labels_as_unverified() -> None:
    entry = TranscriptEntry.model_validate(
        {
            "at": "2026-10-05T10:00:00+00:00",
            "id": "t1",
            "spoken_at": "2026-10-05T10:00:00+00:00",
            "source": "meeting_audio",
            "text": "The budget is fine.",
            "participant_id": "speaker-b",
            "speaker_evidence_id": "voice-2",
        }
    )
    [window] = meeting_windows([entry])
    assert window[0].speaker == "Speaker B with unverified identity"


def test_report_counts_voice_unknown_reasons() -> None:
    rows = [
        {
            "at": "2026-10-05T10:00:00+00:00",
            "event": "transcript_finalized",
            "meeting_id": "m1",
            "source": "stt",
            "transcript_id": transcript_id,
            "participant_id": participant_id,
            "speaker_evidence_id": evidence_id,
            "speaker_reason": reason,
        }
        for transcript_id, participant_id, evidence_id, reason in (
            ("t1", "speaker-a", "voice-1", "dominant_slot"),
            ("t2", "unknown", "voice-2", "overlap"),
            ("t3", "unknown", "voice-3", "overlap"),
        )
    ]
    participation = build_session_report(rows)["participation"]
    assert participation["attributed_utterances"] == 1
    assert participation["unknown_reasons"] == {"overlap": 2}


def test_voice_label_replay_gate_on_probe_attr_8() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    summary = replay_voice_labels(fixture)
    assert summary["solo_turns"] == 36
    assert summary["label_stability"] >= LABEL_STABILITY_GATE
    assert summary["overlap_unknown_rate"] >= OVERLAP_UNKNOWN_GATE
    assert summary["coverage"] >= 0.9
    assert summary["purity"] >= LABEL_STABILITY_GATE
    join_labels = summary["join_labels"]
    assert sorted(join_labels) == ["Guest 1", "Guest 2", "Guest 3", "Host"]
    assert len(set(join_labels.values())) == len(join_labels)


@pytest.mark.browser
async def test_voice_script_taps_remote_audio_and_skips_local_microphone() -> None:
    fake_microphone = (
        "navigator.mediaDevices.getUserMedia = async () => {"
        " const ctx = new AudioContext();"
        " const tone = ctx.createOscillator();"
        " const sink = ctx.createMediaStreamDestination();"
        " tone.connect(sink); tone.start(); ctx.resume();"
        " return sink.stream; };"
    )
    page_script = (
        "(async () => {"
        " const ctx = new AudioContext();"
        " const remote = ctx.createMediaStreamDestination();"
        " const tone = ctx.createOscillator(); tone.connect(remote); tone.start();"
        " ctx.createMediaStreamSource(remote.stream);"
        " const mic = await navigator.mediaDevices.getUserMedia({audio: true});"
        " ctx.createMediaStreamSource(new MediaStream([mic.getAudioTracks()[0].clone()]));"
        " ctx.resume();"
        "})();"
    )
    async with open_direct_page(headless=True) as page:
        await page.add_init_script(fake_microphone)
        await page.add_init_script(load_voice_activity_script())
        await page.route(
            "https://voice.fakeyou.test/",
            lambda route: route.fulfill(
                body="<div data-participant-id='devices/1'>"
                "<div data-participant-id='devices/1'></div>"
                "<span class='notranslate'>Rani</span></div>"
                f"<script>{page_script}</script>",
                content_type="text/html",
            ),
        )
        await page.goto("https://voice.fakeyou.test/", wait_until="load")
        levels: list[list[float]] = []
        drained: dict[str, Any] = {}
        deadline = asyncio.get_running_loop().time() + 15.0
        while asyncio.get_running_loop().time() < deadline:
            drained = await page.evaluate(VOICE_ACTIVITY_DRAIN_JS)
            levels += drained["levels"]
            if drained["local"] and len([level for level in levels if level[2] > 0.1]) >= 10:
                break
            await asyncio.sleep(0.2)
        assert drained["local"] == [1]
        assert {int(slot) for _, slot, level in levels if level > 0.1} == {0}
        assert drained["participants"] == [{"id": "devices/1", "name": "Rani"}]
        assert isinstance(drained["instance"], str) and drained["instance"]


def frames_batch(page_ms: float, frames: list[list[Any]], **extra: Any) -> dict[str, Any]:
    return {**batch(page_ms, [], **extra), "frames": frames}


def test_csrc_mode_labels_the_participant_on_the_dominant_slot() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(
            11_000.0,
            [[10_300.0, 0, 0.05, [42, 111]], [10_300.0, 1, 0.4, [42, 222]]],
        ),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    attribution = tracker.attribute()
    assert (attribution.participant_id, attribution.reason, attribution.slot) == (
        "speaker-a",
        "dominant_csrc",
        1,
    )
    assert attribution.journal_fields()["speaker_csrc"] == [222]


def test_csrc_label_follows_the_participant_across_slots() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(11_000.0, [[10_300.0, 0, 0.4, [42, 111]], [10_350.0, 1, 0.02, [42, 222]]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-a"
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        frames_batch(13_000.0, [[12_300.0, 2, 0.4, [42, 111]], [12_300.0, 1, 0.02, [42, 222]]]),
        sent_ms=13_000.0,
        received_ms=13_000.0,
    )
    moved = tracker.attribute()
    assert (moved.participant_id, moved.slot) == ("speaker-a", 2)


def test_csrc_label_survives_the_marker_being_learned_later() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(11_000.0, [[10_300.0, 0, 0.4, [42, 111]]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-a"
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        frames_batch(13_000.0, [[12_300.0, 1, 0.4, [42, 222]]]),
        sent_ms=13_000.0,
        received_ms=13_000.0,
    )
    second = tracker.attribute()
    assert (second.participant_id, second.journal_fields()["speaker_csrc"]) == (
        "speaker-b",
        [222],
    )
    speak(tracker, clock, 14.0, 15.0)
    tracker.observe_batch(
        frames_batch(15_000.0, [[14_300.0, 0, 0.4, [42, 111]]]),
        sent_ms=15_000.0,
        received_ms=15_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-a"


def test_csrc_mode_reports_overlap_and_conflict_as_unknown() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(11_000.0, [[10_300.0, 0, 0.3, [42, 111]], [10_300.0, 1, 0.25, [42, 222]]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    assert tracker.attribute().reason == "overlap"
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        frames_batch(13_000.0, [[12_200.0, 0, 0.4, [42, 111]], [12_250.0, 1, 0.01, [42, 222]]]),
        sent_ms=13_000.0,
        received_ms=13_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-a"
    speak(tracker, clock, 14.0, 15.0)
    tracker.observe_batch(
        frames_batch(15_000.0, [[14_200.0, 1, 0.4, [42, 222]]]),
        sent_ms=15_000.0,
        received_ms=15_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-b"
    speak(tracker, clock, 16.0, 17.0)
    tracker.observe_batch(
        frames_batch(17_000.0, [[16_200.0, 0, 0.4, [42, 111]], [16_500.0, 0, 0.4, [42, 222]]]),
        sent_ms=17_000.0,
        received_ms=17_000.0,
    )
    assert tracker.attribute().reason == "csrc_conflict"


def test_csrc_mode_ignores_webaudio_taps_and_twin_playback() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    tracker.observe_self_speech(True)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_self_speech(False)
    tracker.observe_batch(
        {
            **frames_batch(11_000.0, [[10_300.0, 0, 0.3, [42, 111]]]),
            "levels": [[10_300.0, 5, 0.9]],
        },
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    attribution = tracker.attribute()
    assert (attribution.participant_id, attribution.slot) == ("speaker-a", 0)


def test_csrc_mode_shifts_frames_by_their_own_lag() -> None:
    clock = FakeClock(now=10.0)
    tracker = VoiceActivityTracker(twin_name="Twin", monotonic=clock.monotonic, frame_lag_ms=500.0)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(11_000.0, [[9_600.0, 0, 0.3, [111]], [10_800.0, 1, 0.3, [222]]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    assert tracker.attribute().slot == 0


def test_csrc_mode_keeps_labels_across_page_reload() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(11_000.0, [[10_300.0, 0, 0.3, [111]]]), sent_ms=11_000.0, received_ms=11_000.0
    )
    assert tracker.attribute().participant_id == "speaker-a"
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        {**frames_batch(500.0, [[-200.0, 0, 0.3, [111]]]), "instance": "i2"},
        sent_ms=13_000.0,
        received_ms=13_000.0,
    )
    assert tracker.attribute().participant_id == "speaker-a"


@pytest.mark.browser
async def test_voice_script_reads_level_and_csrc_from_encoded_frames() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, args=["--autoplay-policy=no-user-gesture-required"]
        )
        try:
            page = await browser.new_page()
            await page.add_init_script(load_voice_activity_script())
            await page.goto("about:blank")
            await page.evaluate(
                "async () => {"
                " const ctx = new AudioContext(); await ctx.resume();"
                " const tone = ctx.createOscillator();"
                " const out = ctx.createMediaStreamDestination();"
                " tone.connect(out); tone.start();"
                " const a = new RTCPeerConnection();"
                " const b = new RTCPeerConnection({encodedInsertableStreams: true});"
                " a.onicecandidate = (e) => e.candidate && b.addIceCandidate(e.candidate);"
                " b.onicecandidate = (e) => e.candidate && a.addIceCandidate(e.candidate);"
                " b.ontrack = (e) => {"
                "  const streams = e.receiver.createEncodedStreams();"
                "  streams.readable.pipeTo(streams.writable); };"
                " a.addTrack(out.stream.getAudioTracks()[0], out.stream);"
                " await a.setLocalDescription(await a.createOffer());"
                " await b.setRemoteDescription(a.localDescription);"
                " await b.setLocalDescription(await b.createAnswer());"
                " await a.setRemoteDescription(b.localDescription);"
                "}"
            )
            frames: list[list[Any]] = []
            deadline = asyncio.get_running_loop().time() + 15.0
            while len(frames) < 10 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.3)
                frames += (await page.evaluate(VOICE_ACTIVITY_DRAIN_JS))["frames"]
            assert len(frames) >= 10
            assert {frame[1] for frame in frames} == {0}
            assert max(frame[2] for frame in frames) > 0.1
            assert all(frame[3] == [] for frame in frames)
        finally:
            await browser.close()


def test_voice_fixture_carries_encoded_frames(tmp_path: Path) -> None:
    probe = tmp_path / "probe.jsonl"
    probe.write_text(
        json.dumps(
            {
                "kind": "page",
                "page_ms": 1000.0,
                "mono_ms": 5000.0,
                "wall": "2026-10-05T10:00:00+00:00",
                "levels": [],
                "frames": [
                    {"page_ms": 900.0, "receiver": 1, "level": 0.4, "csrc": [42, 7]},
                    {"page_ms": 950.0, "receiver": 1},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record = tmp_path / "record.jsonl"
    record.write_text("", encoding="utf-8")
    truth = tmp_path / "truth.json"
    truth.write_text("[]", encoding="utf-8")
    fixture = voice_fixture_from_probe(
        probe, record, truth, twin_name="Twin", aliases={}, persons={}
    )
    assert fixture["events"][0]["batch"]["frames"] == [[900.0, 1, 0.4, [42, 7]]]


def test_csrc_label_replay_gate_on_live_session() -> None:
    fixture = json.loads(CSRC_FIXTURE.read_text(encoding="utf-8"))
    summary = replay_voice_labels(fixture)
    assert summary["solo_turns"] == 11
    assert summary["coverage"] >= 0.9
    assert summary["label_stability"] >= LABEL_STABILITY_GATE
    assert summary["purity"] >= LABEL_STABILITY_GATE
    assert summary["overlap_unknown_rate"] >= OVERLAP_UNKNOWN_GATE
    assert summary["person_labels"] == {"A": ["speaker-a"], "B": ["speaker-b"]}
    assert set(summary["reasons"]) == {"dominant_csrc"}


def test_binder_fleet_capacity_binds_three_remote_streams_stably() -> None:
    from fakeyou.stream_capture import STREAM_FLEET_SLOTS

    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    binder.update_roster(["remote-a", "remote-b", "remote-c"])
    binder.observe_stream(0, [111])
    binder.observe_stream(1, [222])
    binder.observe_stream(2, [333])
    labels = []
    for slot in (0, 1, 2):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 3
    for slot in (0, 1, 2):
        repeat = binder.resolve(slot)
        assert not isinstance(repeat, str)
        assert repeat.label == labels[slot]


def test_binder_three_roster_binds_without_contention() -> None:
    binder = SlotBinder()
    binder.update_roster([f"remote-{index}" for index in range(MEET_AUDIO_SLOTS)])
    binder.observe_stream(0, [111])
    binder.observe_stream(1, [222])
    binder.observe_stream(2, [333])
    labels = []
    for slot in (0, 1, 2):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == MEET_AUDIO_SLOTS


def test_binder_rejoin_preserves_bound_identity_across_slot_move() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    binder.observe_stream(0, [111])
    first = binder.resolve(0)
    assert not isinstance(first, str)
    assert (first.label, first.device) == ("A", "host")
    binder.update_roster([])
    binder.update_roster(["host"])
    binder.observe_stream(1, [111])
    moved = binder.resolve(1)
    assert not isinstance(moved, str)
    assert moved.label == "A"


def test_binder_keeps_active_speakers_when_lurkers_overflow_roster() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    first = binder.resolve(3)
    assert not isinstance(first, str)
    binder.update_roster(["host", "guest"])
    second = binder.resolve(4)
    assert not isinstance(second, str)
    binder.update_roster(["host", "guest", "lurker-a", "lurker-b", "lurker-c", "lurker-d"])
    kept_first = binder.resolve(3)
    kept_second = binder.resolve(4)
    assert not isinstance(kept_first, str)
    assert not isinstance(kept_second, str)
    assert (kept_first.label, kept_second.label) == (first.label, second.label)


def test_binder_allows_new_speakers_while_roster_overflows_capacity() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    first = binder.resolve(3)
    assert not isinstance(first, str)
    assert first.label == "A"
    binder.update_roster(["host"] + [f"lurker-{index}" for index in range(MEET_AUDIO_SLOTS + 1)])
    outcome = binder.resolve(9)
    assert not isinstance(outcome, str)
    assert outcome.label == "B"
    kept = binder.resolve(3)
    assert not isinstance(kept, str)
    assert kept.label == "A"


def test_binder_distinct_sync_sources_keep_per_slot_labels_without_follow() -> None:
    binder = SlotBinder()
    binder.update_roster(["host", "guest", "visitor"])
    binder.observe_stream(0, [6666])
    binder.observe_stream(1, [6667])
    binder.observe_stream(2, [6668])
    first = binder.resolve(0)
    second = binder.resolve(1)
    third = binder.resolve(2)
    assert not isinstance(first, str)
    assert not isinstance(second, str)
    assert not isinstance(third, str)
    assert len({first.label, second.label, third.label}) == 3
    kept_first = binder.resolve(0)
    kept_second = binder.resolve(1)
    kept_third = binder.resolve(2)
    assert not isinstance(kept_first, str)
    assert not isinstance(kept_second, str)
    assert not isinstance(kept_third, str)
    assert kept_first.label == first.label
    assert kept_second.label == second.label
    assert kept_third.label == third.label


def test_binder_binds_four_streams_when_five_share_the_roster() -> None:
    from fakeyou.stream_capture import STREAM_FLEET_SLOTS

    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    binder.update_roster([f"devices/{index}" for index in range(5)])
    for slot in range(4):
        binder.observe_stream(slot, [111 + slot])
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 4
    for slot in range(4):
        repeat = binder.resolve(slot)
        assert not isinstance(repeat, str)
        assert repeat.label == labels[slot]


def test_binder_never_binds_self_entry_to_a_speaking_slot() -> None:
    from fakeyou.stream_capture import STREAM_FLEET_SLOTS

    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    binder.update_roster(["devices/self"] + [f"devices/{index}" for index in range(4)])
    for slot in range(4):
        binder.observe_stream(slot, [111 + slot])
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        assert outcome.device != "devices/self"
        labels.append(outcome.label)
    assert len(set(labels)) == 4


def test_binder_binds_speakers_with_lurker_in_roster() -> None:
    from fakeyou.stream_capture import STREAM_FLEET_SLOTS

    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    binder.update_roster([f"devices/{index}" for index in range(5)] + ["devices/lurker"])
    for slot in range(4):
        binder.observe_stream(slot, [111 + slot])
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 4
    for slot in range(4):
        repeat = binder.resolve(slot)
        assert not isinstance(repeat, str)
        assert repeat.label == labels[slot]


def test_binder_departure_releases_only_one_tenure() -> None:
    from fakeyou.stream_capture import STREAM_FLEET_SLOTS

    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    binder.update_roster([f"devices/{index}" for index in range(5)])
    for slot in range(4):
        binder.observe_stream(slot, [111 + slot])
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 4
    binder.update_roster([f"devices/{index}" for index in range(4)])
    kept = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        kept.append(outcome.label)
    assert kept[1:] == labels[1:]
    assert kept[0] != labels[0]


def test_binder_refuses_new_slot_when_demand_reaches_capacity() -> None:
    binder = SlotBinder()
    binder.update_roster(["host", "guest", "visitor"])
    for slot in (3, 4, 5):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
    assert binder.resolve(6) == "slot_contention"
    kept = binder.resolve(3)
    assert not isinstance(kept, str)
    assert kept.label == "A"


def test_tracker_binds_sequential_slots_when_twin_names_collide() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    roster = [{"id": f"devices/{index}", "name": "Twin"} for index in range(5)]
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [[10_400.0, 3, 0.3]], roster), sent_ms=11_000.0, received_ms=11_000.0
    )
    first = tracker.attribute()
    assert (first.participant_id, first.slot) == ("speaker-a", 3)
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        batch(13_000.0, [[12_400.0, 4, 0.3]], roster), sent_ms=13_000.0, received_ms=13_000.0
    )
    second = tracker.attribute()
    assert (second.participant_id, second.slot) == ("speaker-b", 4)


def test_binder_refuses_follow_when_shared_label_spans_two_slots() -> None:
    binder = SlotBinder(labels=iter(["A", "A", "B", "C"]))
    binder.update_roster(["d1", "d2", "d3"])
    first = binder.resolve(0)
    second = binder.resolve(1)
    assert not isinstance(first, str) and not isinstance(second, str)
    assert (first.label, second.label) == ("A", "A")
    binder.observe_stream(0, [111])
    binder.observe_stream(1, [111])
    binder.observe_stream(2, [111])
    outcome = binder.resolve(2)
    if isinstance(outcome, str):
        assert outcome in ("slot_conflict", "slot_contention", "self_overlap")
    else:
        assert outcome.label == "B"


def test_binder_emits_minted_event_on_new_label_only() -> None:
    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    first = binder.resolve(0)
    assert not isinstance(first, str)
    assert binder.drain_binder_events() == [{"event": "tenure_minted", "label": "A", "slot": 0}]
    kept = binder.resolve(0)
    assert not isinstance(kept, str)
    assert binder.drain_binder_events() == []


def test_binder_emits_move_event_on_csrc_follow() -> None:
    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    binder.observe_stream(0, [777])
    first = binder.resolve(0)
    assert not isinstance(first, str)
    assert binder.drain_binder_events() == [{"event": "tenure_minted", "label": "A", "slot": 0}]
    binder.observe_stream(1, [777])
    moved = binder.resolve(1)
    assert not isinstance(moved, str)
    assert moved.label == first.label
    events = binder.drain_binder_events()
    assert events == [
        {
            "event": "tenure_moved",
            "label": first.label,
            "from_slot": 0,
            "to_slot": 1,
            "csrcs": [777],
        }
    ]
    assert binder.drain_binder_events() == []


def test_binder_emits_release_event_on_departure() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    host = binder.resolve(3)
    assert not isinstance(host, str)
    assert binder.drain_binder_events() == [{"event": "tenure_minted", "label": "A", "slot": 3}]
    binder.update_roster([])
    events = binder.drain_binder_events()
    assert events == [
        {"event": "tenure_released", "label": "A", "slot": 3, "device": "host", "dormant": True}
    ]


def test_binder_flags_relabel_when_delete_one_releases_present_slot() -> None:
    binder = SlotBinder()
    binder.update_roster(["a", "b", "c"])
    first = binder.resolve(3)
    assert not isinstance(first, str)
    assert binder.drain_binder_events() == [{"event": "tenure_minted", "label": "A", "slot": 3}]
    binder.update_roster(["a", "b"])
    release = binder.drain_binder_events()
    assert release == [
        {"event": "tenure_released", "label": "A", "slot": 3, "device": None, "dormant": False}
    ]
    replaced = binder.resolve(3)
    assert not isinstance(replaced, str)
    assert replaced.label == "B"
    fault = binder.drain_binder_events()
    assert fault == [
        {"event": "tenure_minted", "label": "B", "slot": 3},
        {"event": "tenure_relabel", "slot": 3, "old_label": "A", "new_label": "B"},
    ]


def test_binder_reset_clears_labels_and_events() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    first = binder.resolve(0)
    assert not isinstance(first, str)
    binder.update_roster([])
    binder.reset()
    binder.update_roster(["host"])
    outcome = binder.resolve(0)
    assert not isinstance(outcome, str)
    assert binder.drain_binder_events() == [{"event": "tenure_minted", "label": "B", "slot": 0}]


def test_binder_defers_dormant_restore_while_slot_occupied() -> None:
    binder = SlotBinder()
    binder.update_roster(["host"])
    host = binder.resolve(0)
    assert not isinstance(host, str)
    assert host.device == "host"
    binder.update_roster(["host", "guest"])
    assert binder.drain_binder_events() == [{"event": "tenure_minted", "label": "A", "slot": 0}]
    binder.update_roster(["guest"])
    assert binder.drain_binder_events() != []
    other = binder.resolve(0)
    assert not isinstance(other, str)
    assert other.label != host.label
    binder.update_roster(["guest", "host"])
    assert binder.resolve(0) is other
    binder.update_roster(["host"])
    restored = binder.resolve(0)
    assert not isinstance(restored, str)
    assert restored.label == host.label


def test_tracker_binds_two_humans_with_unique_names() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    roster = [
        {"id": "devices/self", "name": "Twin"},
        {"id": "devices/1", "name": "Rani"},
        {"id": "devices/2", "name": "Budi"},
    ]
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        batch(11_000.0, [[10_400.0, 3, 0.3]], roster), sent_ms=11_000.0, received_ms=11_000.0
    )
    first = tracker.attribute()
    assert (first.participant_id, first.slot) == ("speaker-a", 3)
    speak(tracker, clock, 12.0, 13.0)
    tracker.observe_batch(
        batch(13_000.0, [[12_400.0, 4, 0.3]], roster), sent_ms=13_000.0, received_ms=13_000.0
    )
    second = tracker.attribute()
    assert (second.participant_id, second.slot) == ("speaker-b", 4)


def test_binder_adapter_exposes_registry_uuid() -> None:
    from fakeyou.identity import SpeakerRegistry

    registry = SpeakerRegistry()
    registry.update_roster(["dev-a"])
    binder = SlotBinder(registry=registry)
    outcome = binder.resolve(0)
    assert outcome.uuid == registry.resolve(0).uuid
    assert outcome.uuid


def test_binder_adapter_keeps_default_registry_private() -> None:
    first = SlotBinder()
    second = SlotBinder()
    first.update_roster(["dev-a"])
    second.update_roster(["dev-a"])
    assert first.resolve(0).uuid == second.resolve(0).uuid
    assert first.drain_binder_events() != []


def test_tracker_and_binder_share_registry_rows() -> None:
    from fakeyou.identity import SpeakerRegistry

    registry = SpeakerRegistry()
    registry.update_roster(["dev-a", "dev-b"])
    binder = SlotBinder(registry=registry)
    tracker = VoiceActivityTracker(twin_name="Twin", registry=registry)
    binder.observe_stream(0, [11])
    row = binder.resolve(0)
    same = tracker._registry.resolve_csrcs(frozenset([11]))
    assert same.uuid == row.uuid


def test_csrc_attribution_carries_speaker_uuid() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.0, 11.0)
    tracker.observe_batch(
        frames_batch(11_000.0, [[10_300.0, 0, 0.4, [42, 111]]]),
        sent_ms=11_000.0,
        received_ms=11_000.0,
    )
    attribution = tracker.attribute()
    assert attribution.speaker_uuid
    assert attribution.journal_fields()["speaker_uuid"] == attribution.speaker_uuid


def test_dominant_slot_attribution_carries_speaker_uuid() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.2, 11.2)
    clock.now = 11.4
    tracker.observe_batch(
        batch(1000.0 + 11_400.0, [[1000.0 + 10_050.0, 3, 0.3], [1000.0 + 10_100.0, 4, 0.05]]),
        sent_ms=11_400.0,
        received_ms=11_400.0,
    )
    attribution = tracker.attribute()
    assert attribution.reason == "dominant_slot"
    assert attribution.speaker_uuid
    assert attribution.journal_fields()["speaker_uuid"] == attribution.speaker_uuid
