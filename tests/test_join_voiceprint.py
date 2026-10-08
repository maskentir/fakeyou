from __future__ import annotations

import struct
from pathlib import Path
from typing import Any, cast

from pipecat.frames.frames import (
    InputAudioRawFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

import fakeyou.cli.join as join_module
from fakeyou.assembly import create_voiceprint_live
from fakeyou.cli.join import build_transcript_callback
from fakeyou.identity import SpeakerRegistry
from fakeyou.record import MeetingRecord, meeting_record_path
from fakeyou.settings import Settings
from fakeyou.voice_activity import SlotBinder, VoiceAttribution
from tests.fakes import RecordingJournal
from tests.test_assembly_voiceprint import FakeConnection
from tests.test_voiceprint_match import FakeEmbedder, make_record


def program_print(conn: FakeConnection) -> None:
    record = make_record("alice", (1.0, 0.0))
    conn.results.append(
        [
            (
                record.person_id,
                struct.pack("<2f", *record.embedding),
                record.dim,
                record.n_utterances,
                record.consent_ts,
                record.policy_version,
                record.is_negative,
                record.enrolled_at,
                record.last_used_at,
                record.owner_name,
            )
        ]
    )


async def prime_turn_tap(live: Any, stream_id: str, payload: bytes) -> None:
    tap = live.tap_for(stream_id)
    pushed: list[Any] = []

    async def sink(frame: Any, direction: Any) -> None:
        pushed.append(frame)
        return None

    tap.push_frame = sink
    await tap.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await tap.process_frame(
        InputAudioRawFrame(audio=payload, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await tap.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)


def bound_binder() -> tuple[SpeakerRegistry, SlotBinder]:
    registry = SpeakerRegistry()
    binder = SlotBinder(registry=registry)
    binder.update_roster(["dev-a"])
    binder.observe_stream(0, [7])
    return registry, binder


class _ScriptedVoiceActivity:
    def __init__(self, attributions: list[VoiceAttribution]) -> None:
        self._attributions = list(attributions)

    def attribute(self) -> VoiceAttribution:
        return self._attributions.pop(0)


async def test_final_scores_turn_and_notes_honest_margin(
    settings: Settings, tmp_path: Path
) -> None:
    settings.identity.voiceprint_enabled = True
    conn = FakeConnection()
    live = create_voiceprint_live(settings, connect=lambda: conn, embedder=FakeEmbedder((1.0, 0.0)))
    assert live is not None
    program_print(conn)
    await prime_turn_tap(live, "slot-0", b"\x02" * 96000)
    registry, binder = bound_binder()
    journal = RecordingJournal()
    record = MeetingRecord(meeting_record_path(tmp_path, "meeting-9"))
    callback = build_transcript_callback(
        journal=journal,
        record=record,
        conversation=None,
        voice_activity=None,
        binder=binder,
        per_stream_enabled=True,
        voiceprint=live,
        registry=registry,
        faults=None,
    )
    frame = TranscriptionFrame("Hello.", "", "2026-10-06T01:00:03+00:00", finalized=True)
    frame.transport_source = "slot-0"

    await callback(frame)

    [decided] = journal.events("voiceprint_decided")
    assert decided["outcome"] == "verified"
    assert decided["person_id"] == "alice"
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert row.margin == 2.0
    assert live.tap_for("slot-0").take_clip() == b""
    record.close()
    journal.close()
    live.close()


async def test_short_clip_skips_scoring_without_decision(
    settings: Settings, tmp_path: Path
) -> None:
    settings.identity.voiceprint_enabled = True
    conn = FakeConnection()
    live = create_voiceprint_live(settings, connect=lambda: conn, embedder=FakeEmbedder((1.0, 0.0)))
    assert live is not None
    await prime_turn_tap(live, "slot-0", b"\x02" * 320)
    registry, binder = bound_binder()
    journal = RecordingJournal()
    record = MeetingRecord(meeting_record_path(tmp_path, "meeting-9"))
    callback = build_transcript_callback(
        journal=journal,
        record=record,
        conversation=None,
        voice_activity=None,
        binder=binder,
        per_stream_enabled=True,
        voiceprint=live,
        registry=registry,
        faults=None,
    )
    frame = TranscriptionFrame("Hi.", "", "2026-10-06T01:00:03+00:00", finalized=True)
    frame.transport_source = "slot-0"

    await callback(frame)

    assert journal.events("voiceprint_decided") == []
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert row.margin == 1.0
    record.close()
    journal.close()
    live.close()


async def test_skipped_final_discards_stale_tap_audio_per_stream(
    settings: Settings, tmp_path: Path, monkeypatch: Any
) -> None:
    settings.identity.voiceprint_enabled = True
    conn = FakeConnection()
    live = create_voiceprint_live(settings, connect=lambda: conn, embedder=FakeEmbedder((1.0, 0.0)))
    assert live is not None
    registry = SpeakerRegistry()
    binder = SlotBinder(registry=registry)
    journal = RecordingJournal()
    record = MeetingRecord(meeting_record_path(tmp_path, "meeting-9"))
    callback = build_transcript_callback(
        journal=journal,
        record=record,
        conversation=None,
        voice_activity=None,
        binder=binder,
        per_stream_enabled=True,
        voiceprint=live,
        registry=registry,
        faults=None,
    )
    await prime_turn_tap(live, "slot-0", b"\x0a" * 3200)
    stale = TranscriptionFrame("Stale.", "", "2026-10-06T01:00:03+00:00", finalized=True)
    stale.transport_source = "slot-0"
    await callback(stale)
    binder.update_roster(["dev-a"])
    binder.observe_stream(0, [7])
    await prime_turn_tap(live, "slot-0", b"\x0b" * 3200)
    seen: list[bytes] = []

    def capture(clip: bytes, embedder: Any, matcher: Any, *, min_seconds: float) -> Any:
        seen.append(clip)
        return None

    monkeypatch.setattr(join_module, "score_turn_audio", capture)
    fresh = TranscriptionFrame("Fresh.", "", "2026-10-06T01:00:04+00:00", finalized=True)
    fresh.transport_source = "slot-0"
    await callback(fresh)

    assert seen == [b"\x0b" * 3200]
    record.close()
    journal.close()
    live.close()


async def test_skipped_final_discards_stale_tap_audio_mixed(
    settings: Settings, tmp_path: Path, monkeypatch: Any
) -> None:
    settings.identity.voiceprint_enabled = True
    conn = FakeConnection()
    live = create_voiceprint_live(settings, connect=lambda: conn, embedder=FakeEmbedder((1.0, 0.0)))
    assert live is not None
    registry = SpeakerRegistry()
    voice_activity = _ScriptedVoiceActivity(
        [
            VoiceAttribution("unknown", "no_speech", "voice-1"),
            VoiceAttribution(
                "speaker-a", "dominant_slot", "voice-2", slot=0, bound=True, speaker_uuid="uuid-a"
            ),
        ]
    )
    journal = RecordingJournal()
    record = MeetingRecord(meeting_record_path(tmp_path, "meeting-9"))
    callback = build_transcript_callback(
        journal=journal,
        record=record,
        conversation=None,
        voice_activity=cast(Any, voice_activity),
        binder=SlotBinder(registry=registry),
        per_stream_enabled=False,
        voiceprint=live,
        registry=registry,
        faults=None,
    )
    await prime_turn_tap(live, "", b"\x0a" * 3200)
    await callback(TranscriptionFrame("Stale.", "", "2026-10-06T01:00:03+00:00", finalized=True))
    await prime_turn_tap(live, "", b"\x0b" * 3200)
    seen: list[bytes] = []

    def capture(clip: bytes, embedder: Any, matcher: Any, *, min_seconds: float) -> Any:
        seen.append(clip)
        return None

    monkeypatch.setattr(join_module, "score_turn_audio", capture)
    await callback(TranscriptionFrame("Fresh.", "", "2026-10-06T01:00:04+00:00", finalized=True))

    assert seen == [b"\x0b" * 3200]
    record.close()
    journal.close()
    live.close()


async def test_disabled_voiceprint_never_scores(settings: Settings, tmp_path: Path) -> None:
    registry, binder = bound_binder()
    journal = RecordingJournal()
    record = MeetingRecord(meeting_record_path(tmp_path, "meeting-9"))
    callback = build_transcript_callback(
        journal=journal,
        record=record,
        conversation=None,
        voice_activity=None,
        binder=binder,
        per_stream_enabled=True,
    )
    frame = TranscriptionFrame("Hello.", "", "2026-10-06T01:00:03+00:00", finalized=True)
    frame.transport_source = "slot-0"

    await callback(frame)

    assert journal.events("voiceprint_decided") == []
    assert [row["event"] for row in journal.events()] == ["transcript_finalized"]
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert row.margin == 1.0
    record.close()
    journal.close()
