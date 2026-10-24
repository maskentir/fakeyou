from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast, override

import pytest

import fakeyou.orchestration.transcript as transcript_module
from fakeyou.audio.voice_activity import SlotBinder, VoiceAttribution
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.db.voiceprint_store import VoiceprintRecord
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.identity.identity import SpeakerRegistry
from fakeyou.identity.verify import CalibrationArtifact
from fakeyou.orchestration.assembly import VoiceprintLive, build_voiceprint_live
from fakeyou.orchestration.transcript import (
    DrainOptions,
    LiveScoring,
    TranscriptSinks,
    build_transcript_callback,
)
from fakeyou.persistence.record import MeetingRecord, meeting_record_path
from tests._persistence_fakes import RecordingJournal
from tests._voiceprint_fakes import FakeEmbedder, InMemoryVoiceprints, make_record


def build_live(
    settings: Settings,
    records: list[VoiceprintRecord],
    embedder: FakeEmbedder,
    artifact: CalibrationArtifact | None = None,
) -> VoiceprintLive:
    return build_voiceprint_live(
        settings, store=InMemoryVoiceprints(records), embedder=embedder, artifact=artifact
    )


def prime_turn_tap(live: Any, stream_id: str, payload: bytes) -> None:
    tap = live.tap_for(stream_id)
    tap.speech_started()
    tap.feed(payload)
    tap.speech_stopped()


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


class _SlowEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__((1.0, 0.0))

    @override
    def embed(self, utterances: Sequence[Sequence[float]]) -> tuple[float, ...]:
        time.sleep(0.5)
        return super().embed(utterances)


async def test_final_scores_turn_and_notes_honest_margin(
    settings: Settings, tmp_path: Path
) -> None:
    live = build_live(settings, [make_record("alice", (1.0, 0.0))], FakeEmbedder((1.0, 0.0)))
    prime_turn_tap(live, "slot-0", b"\x02" * 96000)
    registry, binder = bound_binder()
    journal = RecordingJournal()
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-9")))
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None, faults=None),
        binder=binder,
        voice_activity=None,
        scoring=LiveScoring(live, registry),
        options=DrainOptions(per_stream_enabled=True),
    )
    frame = TranscriptUpdate(text="Hello.", final=True, timestamp="2026-10-06T01:00:03+00:00")
    frame = replace(frame, stream_id="slot-0")

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


async def test_scoring_timeout_records_a_fault_and_keeps_the_callback_alive(
    settings: Settings, tmp_path: Path
) -> None:
    settings.identity.voiceprint_timeout_seconds = 0.01
    live = build_live(settings, [make_record("alice", (1.0, 0.0))], _SlowEmbedder())
    prime_turn_tap(live, "slot-0", b"\x02" * 96000)
    registry, binder = bound_binder()
    journal = RecordingJournal()
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-9")))
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None, faults=None),
        binder=binder,
        voice_activity=None,
        scoring=LiveScoring(live, registry),
        options=DrainOptions(per_stream_enabled=True),
    )
    frame = TranscriptUpdate(text="Hello.", final=True, timestamp="2026-10-06T01:00:03+00:00")
    frame = replace(frame, stream_id="slot-0")

    await callback(frame)

    [fault] = journal.events("fault_recorded")
    assert fault["detail"] == "voiceprint_scoring_timeout: TimeoutError"
    assert journal.events("transcript_finalized") != []
    record.close()
    journal.close()


async def test_short_clip_skips_scoring_without_decision(
    settings: Settings, tmp_path: Path
) -> None:
    live = build_live(settings, [], FakeEmbedder((1.0, 0.0)))
    prime_turn_tap(live, "slot-0", b"\x02" * 320)
    registry, binder = bound_binder()
    journal = RecordingJournal()
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-9")))
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None, faults=None),
        binder=binder,
        voice_activity=None,
        scoring=LiveScoring(live, registry),
        options=DrainOptions(per_stream_enabled=True),
    )
    frame = TranscriptUpdate(text="Hi.", final=True, timestamp="2026-10-06T01:00:03+00:00")
    frame = replace(frame, stream_id="slot-0")

    await callback(frame)

    assert journal.events("voiceprint_decided") == []
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert row.margin == 1.0
    record.close()
    journal.close()


async def test_skipped_final_discards_stale_tap_audio_per_stream(
    settings: Settings, tmp_path: Path, monkeypatch: Any
) -> None:
    live = build_live(settings, [], FakeEmbedder((1.0, 0.0)))
    registry = SpeakerRegistry()
    binder = SlotBinder(registry=registry)
    journal = RecordingJournal()
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-9")))
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None, faults=None),
        binder=binder,
        voice_activity=None,
        scoring=LiveScoring(live, registry),
        options=DrainOptions(per_stream_enabled=True),
    )
    prime_turn_tap(live, "slot-0", b"\x0a" * 3200)
    stale = TranscriptUpdate(text="Stale.", final=True, timestamp="2026-10-06T01:00:03+00:00")
    stale = replace(stale, stream_id="slot-0")
    await callback(stale)
    binder.update_roster(["dev-a"])
    binder.observe_stream(0, [7])
    prime_turn_tap(live, "slot-0", b"\x0b" * 3200)
    seen: list[bytes] = []

    async def capture(
        clip: bytes, embedder: Any, matcher: Any, *, min_seconds: float, timeout_seconds: float
    ) -> Any:
        del embedder
        del matcher
        del min_seconds
        del timeout_seconds
        seen.append(clip)
        return None

    monkeypatch.setattr(transcript_module, "score_turn_audio_async", capture)
    fresh = TranscriptUpdate(text="Fresh.", final=True, timestamp="2026-10-06T01:00:04+00:00")
    fresh = replace(fresh, stream_id="slot-0")
    await callback(fresh)

    assert seen == [b"\x0b" * 3200]
    record.close()
    journal.close()


async def test_skipped_final_discards_stale_tap_audio_mixed(
    settings: Settings, tmp_path: Path, monkeypatch: Any
) -> None:
    live = build_live(settings, [], FakeEmbedder((1.0, 0.0)))
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
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-9")))
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None, faults=None),
        binder=SlotBinder(registry=registry),
        voice_activity=cast(Any, voice_activity),
        scoring=LiveScoring(live, registry),
        options=DrainOptions(per_stream_enabled=False),
    )
    prime_turn_tap(live, "", b"\x0a" * 3200)
    await callback(
        TranscriptUpdate(text="Stale.", final=True, timestamp="2026-10-06T01:00:03+00:00")
    )
    prime_turn_tap(live, "", b"\x0b" * 3200)
    seen: list[bytes] = []

    async def capture(
        clip: bytes, embedder: Any, matcher: Any, *, min_seconds: float, timeout_seconds: float
    ) -> Any:
        del embedder
        del matcher
        del min_seconds
        del timeout_seconds
        seen.append(clip)
        return None

    monkeypatch.setattr(transcript_module, "score_turn_audio_async", capture)
    await callback(
        TranscriptUpdate(text="Fresh.", final=True, timestamp="2026-10-06T01:00:04+00:00")
    )

    assert seen == [b"\x0b" * 3200]
    record.close()
    journal.close()


@pytest.mark.usefixtures("settings")
async def test_disabled_voiceprint_never_scores(tmp_path: Path) -> None:
    registry, binder = bound_binder()
    journal = RecordingJournal()
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-9")))
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None),
        binder=binder,
        voice_activity=None,
        options=DrainOptions(per_stream_enabled=True),
    )
    frame = TranscriptUpdate(text="Hello.", final=True, timestamp="2026-10-06T01:00:03+00:00")
    frame = replace(frame, stream_id="slot-0")

    await callback(frame)

    assert journal.events("voiceprint_decided") == []
    assert [row["event"] for row in journal.events()] == ["transcript_finalized"]
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert row.margin == 1.0
    record.close()
    journal.close()


async def test_live_turn_binds_verified_name_when_claim_matches_voice(
    settings: Settings, tmp_path: Path
) -> None:
    artifact = CalibrationArtifact(
        theta_llr=-10.0,
        margin_floor=-10.0,
        snorm_cohort=(0.0, 1.0),
        platt_a=1.0,
        platt_b=0.0,
        platt_version="v1",
        report_id="test",
    )
    embedder = FakeEmbedder((1.0, 0.0))
    embedder.model_id = "test-model"
    live = build_live(
        settings,
        [make_record("alice", (1.0, 0.0), owner_name="Maya")],
        embedder,
        artifact,
    )
    prime_turn_tap(live, "slot-0", b"\x02" * 96000)
    registry, binder = bound_binder()
    binder.note_roster([{"id": "dev-a", "name": "Maya"}])
    journal = RecordingJournal()
    record_path = meeting_record_path(tmp_path, SessionId("meeting-9"))
    record = MeetingRecord(record_path)
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None, faults=None),
        binder=binder,
        voice_activity=None,
        scoring=LiveScoring(live, registry),
        options=DrainOptions(per_stream_enabled=True),
    )
    frame = TranscriptUpdate(text="Hello.", final=True, timestamp="2026-10-06T01:00:03+00:00")
    frame = replace(frame, stream_id="slot-0")

    await callback(frame)

    [verified] = journal.events("name_verified")
    assert verified["enrollment_id"] == "alice"
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert row.verified_name == "Maya"
    record.close()
    journal.close()
    transcripts = [
        row
        for line in record_path.read_text(encoding="utf-8").splitlines()
        if (row := json.loads(line))["kind"] == "transcript"
    ]
    assert [row["display_claim"] for row in transcripts] == ["Maya"]


async def test_mixed_stream_transcript_carries_display_claim(tmp_path: Path) -> None:
    registry = SpeakerRegistry()
    binder = SlotBinder(registry=registry)
    binder.update_roster(["dev-a"])
    binder.observe_stream(0, [7])
    resolved = binder.resolve(0)
    assert not isinstance(resolved, str)
    binder.note_roster([{"id": "dev-a", "name": "Maya"}])
    voice_activity = _ScriptedVoiceActivity(
        [
            VoiceAttribution(
                "speaker-a",
                "dominant_slot",
                "voice-1",
                slot=0,
                bound=True,
                speaker_uuid=resolved.uuid,
            ),
        ]
    )
    journal = RecordingJournal()
    record_path = meeting_record_path(tmp_path, SessionId("meeting-9"))
    record = MeetingRecord(record_path)
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None, faults=None),
        binder=binder,
        voice_activity=cast(Any, voice_activity),
        scoring=LiveScoring(None, registry),
        options=DrainOptions(per_stream_enabled=False),
    )
    frame = TranscriptUpdate(text="Hi.", final=True, timestamp="2026-10-06T01:00:03+00:00")

    await callback(frame)

    record.close()
    journal.close()
    transcripts = [
        row
        for line in record_path.read_text(encoding="utf-8").splitlines()
        if (row := json.loads(line))["kind"] == "transcript"
    ]
    assert [row["display_claim"] for row in transcripts] == ["Maya"]


async def test_scoring_bug_records_a_fault_then_propagates(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = build_live(settings, [make_record("alice", (1.0, 0.0))], FakeEmbedder((1.0, 0.0)))
    prime_turn_tap(live, "slot-0", b"\x02" * 96000)
    registry, binder = bound_binder()
    journal = RecordingJournal()
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-9")))
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None, faults=None),
        binder=binder,
        voice_activity=None,
        scoring=LiveScoring(live, registry),
        options=DrainOptions(per_stream_enabled=True),
    )

    async def boom(*args: Any, **kwargs: Any) -> Any:
        del args
        del kwargs
        raise KeyError("scoring bug")

    monkeypatch.setattr(transcript_module, "score_turn_audio_async", boom)
    frame = TranscriptUpdate(text="Hello.", final=True, timestamp="2026-10-06T01:00:03+00:00")
    frame = replace(frame, stream_id="slot-0")

    with pytest.raises(KeyError, match="scoring bug"):
        await callback(frame)

    [fault] = journal.events("fault_recorded")
    assert fault["detail"] == "voiceprint_scoring: KeyError"
    record.close()
    journal.close()


async def test_scoring_operational_error_records_a_fault_and_keeps_callback_alive(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = build_live(settings, [make_record("alice", (1.0, 0.0))], FakeEmbedder((1.0, 0.0)))
    prime_turn_tap(live, "slot-0", b"\x02" * 96000)
    registry, binder = bound_binder()
    journal = RecordingJournal()
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-9")))
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None, faults=None),
        binder=binder,
        voice_activity=None,
        scoring=LiveScoring(live, registry),
        options=DrainOptions(per_stream_enabled=True),
    )

    async def boom(*args: Any, **kwargs: Any) -> Any:
        del args
        del kwargs
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(transcript_module, "score_turn_audio_async", boom)
    frame = TranscriptUpdate(text="Hello.", final=True, timestamp="2026-10-06T01:00:03+00:00")
    frame = replace(frame, stream_id="slot-0")

    await callback(frame)

    [fault] = journal.events("fault_recorded")
    assert fault["detail"] == "voiceprint_scoring: RuntimeError"
    record.close()
    journal.close()
