import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from fakeyou.audio.stream_capture import (
    StreamChunk,
)
from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.config.identifiers import SessionId
from fakeyou.foundation.transcript import TranscriptEvent, TranscriptUpdate
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.kernel_port import HeardFinal, KernelPort, TurnSignal
from fakeyou.orchestration.transcript import DrainOptions, TranscriptSinks
from tests._capture_support import recording_registry
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import meet_page
from tests._persistence_fakes import RecordingJournal


class _HeardFinals:
    def __init__(self) -> None:
        self.events: list[TranscriptEvent] = []

    def accept(self, signal: TurnSignal) -> None:
        if isinstance(signal, HeardFinal):
            self.events.append(signal.event)

    async def idle(self) -> None:
        return None


def _heard_finals() -> tuple[KernelPort, _HeardFinals]:
    kernel = KernelPort.for_meeting("meeting-9")
    heard = _HeardFinals()
    kernel.on_turn(heard)
    return kernel, heard


def _chunk(slot: int, seq: int, page_ms: float = 1000.0) -> StreamChunk:
    return StreamChunk(
        slot=slot, seq=seq, csrcs=(111,), level=0.4, page_ms=page_ms, pcm=b"\x00\x01"
    )


async def test_registry_forwards_pcm_to_slot_inputs() -> None:
    from fakeyou.audio.stream_capture import StreamChunk

    registry, made = recording_registry(FakeClock().monotonic)
    await registry.feed(
        StreamChunk(slot=0, seq=0, csrcs=(111,), level=0.4, page_ms=1000.0, pcm=b"\x00\x01")
    )
    await registry.feed(
        StreamChunk(slot=1, seq=0, csrcs=(222,), level=0.5, page_ms=1000.0, pcm=b"\x02\x03")
    )
    await registry.feed(
        StreamChunk(slot=0, seq=1, csrcs=(111,), level=0.6, page_ms=1250.0, pcm=b"\x04\x05")
    )

    assert len(made) == 2
    assert made[0].fed == [b"\x00\x01", b"\x04\x05"]
    assert made[1].fed == [b"\x02\x03"]
    assert set(registry.sessions) == {0, 1}
    assert registry.sessions[0].level == 0.6
    assert registry.sessions[0].page_ms == 1250.0


async def test_registry_releases_session_after_silence() -> None:
    from fakeyou.audio.stream_capture import StreamChunk

    clock = FakeClock(now=1000.0)
    registry, _ = recording_registry(clock.monotonic)
    chunk = StreamChunk(slot=0, seq=0, csrcs=(111,), level=0.4, page_ms=1000.0, pcm=b"\x00\x01")
    await registry.feed(chunk)
    assert set(registry.sessions) == {0}
    clock.now += 29.0
    registry.prune()
    assert set(registry.sessions) == {0}
    clock.now += 2.0
    registry.prune()
    assert dict(registry.sessions) == {}
    await registry.feed(chunk)
    assert set(registry.sessions) == {0}


async def test_registry_feed_journals_drain_gap_detected_and_resync() -> None:
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    registry, _ = recording_registry(FakeClock().monotonic, journal=journal)
    await registry.feed(_chunk(slot=0, seq=0))
    await registry.feed(_chunk(slot=0, seq=2))
    await registry.feed(_chunk(slot=0, seq=0))
    journal.close()
    gaps = [row for line in lines if (row := json.loads(line))["event"] == "drain_gap_detected"]
    assert [(row["kind"], row["missed"]) for row in gaps] == [("gap", 1), ("resync", 0)]


def _frame(text: str, timestamp: str) -> TranscriptUpdate:
    return TranscriptUpdate(text=text, final=False, timestamp=timestamp)


def test_merge_orders_two_streams_chronologically() -> None:
    from fakeyou.foundation.transcript import CaptureContext, merge_transcripts, transcript_event

    first = transcript_event(
        _frame("hello", "2026-10-06T01:00:02+00:00"), "a", None, CaptureContext("s1")
    )
    second = transcript_event(
        _frame("hi", "2026-10-06T01:00:01+00:00"), "b", None, CaptureContext("s0")
    )
    assert [event.text for event in merge_transcripts([first, second])] == ["hi", "hello"]


def test_merge_keeps_stream_tags_stable_on_id() -> None:
    from fakeyou.foundation.transcript import CaptureContext, merge_transcripts, transcript_event

    first = transcript_event(
        _frame("hello", "2026-10-06T01:00:01+00:00"), "a", None, CaptureContext("s1")
    )
    second = transcript_event(
        _frame("hi", "2026-10-06T01:00:01+00:00"), "b", None, CaptureContext("s0")
    )
    merged = merge_transcripts([second, first])
    assert [event.id for event in merged] == sorted([first.id, second.id])
    assert {event.stream_id for event in merged} == {"s0", "s1"}


def test_binder_follows_csrc_across_slot_rotation() -> None:

    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    binder.observe_stream(0, [111])
    binder.observe_stream(1, [222])
    first = binder.resolve(0)
    second = binder.resolve(1)
    assert not isinstance(first, str)
    assert not isinstance(second, str)
    assert first.label != second.label
    binder.observe_stream(2, [111])
    rotated = binder.resolve(2)
    assert not isinstance(rotated, str)
    assert rotated.label == first.label


def test_flag_off_keeps_mixed_path() -> None:
    from fakeyou.config.settings import Settings

    assert Settings().conversation.per_stream_enabled is False


def test_stream_ids_change_transcript_identity() -> None:
    from fakeyou.foundation.transcript import CaptureContext, transcript_event

    left = transcript_event(
        _frame("hello", "2026-10-06T01:00:01+00:00"), "a", None, CaptureContext("s0")
    )
    replay = transcript_event(
        _frame("hello", "2026-10-06T01:00:01+00:00"), "b", None, CaptureContext("s0")
    )
    other = transcript_event(
        _frame("hello", "2026-10-06T01:00:01+00:00"), "a", None, CaptureContext("s1")
    )
    assert left.id == replay.id
    assert left.id != other.id


def test_request_messages_carry_stream_id() -> None:
    from fakeyou.conversation.meeting_messages import request_messages
    from fakeyou.foundation.transcript import CaptureContext, transcript_event

    event = transcript_event(
        _frame("hello", "2026-10-06T01:00:01+00:00"), "a", "e1", CaptureContext("slot-2")
    )
    (message,) = request_messages([event])
    assert message.additional_kwargs["meeting_event"]["stream_id"] == "slot-2"


def test_record_stores_stream_id(tmp_path: Path) -> None:
    from fakeyou.foundation.transcript import CaptureContext, TranscriptUpdate, transcript_event
    from fakeyou.persistence.record import (
        MeetingRecord,
        TranscriptEntry,
        load_meeting_record,
        meeting_record_path,
    )

    path = meeting_record_path(tmp_path, SessionId("meeting-9"))
    record = MeetingRecord(path)
    frame = TranscriptUpdate(text="hello", final=True, timestamp="2026-10-06T01:00:01+00:00")
    record.add_transcript(transcript_event(frame, "a", "e1", CaptureContext("slot-1")))
    record.close()
    [entry] = load_meeting_record(path)
    assert isinstance(entry, TranscriptEntry)
    assert entry.stream_id == "slot-1"
    unslotted = {
        "kind": "transcript",
        "at": "2026-10-06T01:00:02+00:00",
        "seq": 2,
        "id": "event-unslotted",
        "spoken_at": "2026-10-06T01:00:02+00:00",
        "source": "meeting_audio",
        "text": "Legacy.",
        "participant_id": "unknown",
        "speaker_evidence_id": None,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(unslotted) + "\n")
    entries = load_meeting_record(path)
    assert isinstance(entries[1], TranscriptEntry)
    assert entries[1].stream_id == ""


async def test_flag_off_record_matches_stream_mode_except_identity(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from fakeyou.foundation.transcript import TranscriptUpdate
    from fakeyou.observability.journal import Journal
    from fakeyou.orchestration.transcript import build_transcript_callback
    from fakeyou.persistence.record import MeetingRecord, meeting_record_path

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    turns = [
        TranscriptUpdate(
            text="Let us start with the demo.",
            final=True,
            timestamp="2026-10-06T01:00:01+00:00",
        ),
        TranscriptUpdate(
            text="Sounds good, sharing now.",
            final=True,
            timestamp="2026-10-06T01:00:02+00:00",
        ),
    ]

    async def run(
        *, per_stream: bool, tag: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[TranscriptEvent]]:
        lines: list[str] = []
        journal = Journal("meeting-9", emit=lines.append, now=lambda: now, echo=False)
        path = meeting_record_path(tmp_path, SessionId(tag))
        record = MeetingRecord(path, now=lambda: now)
        kernel, heard = _heard_finals()
        callback = build_transcript_callback(
            sinks=TranscriptSinks(journal, record, kernel=kernel),
            binder=SlotBinder(),
            voice_activity=None,
            options=DrainOptions(per_stream_enabled=per_stream),
        )
        for index, frame in enumerate(turns):
            if per_stream:
                frame = replace(frame, stream_id=f"slot-{index}")
            await callback(frame)
        await kernel.settled()
        record.close()
        journal.close()
        rows = [
            row for line in lines if (row := json.loads(line))["event"] == "transcript_finalized"
        ]
        entries = [
            row
            for line in path.read_text(encoding="utf-8").splitlines()
            if (row := json.loads(line))["kind"] != "header"
        ]
        return rows, entries, heard.events

    mixed_rows, mixed_entries, mixed_events = await run(per_stream=False, tag="mixed-9")
    stream_rows, stream_entries, stream_events = await run(per_stream=True, tag="stream-9")
    assert [event.text for event in mixed_events] == [event.text for event in stream_events]
    assert [event.text for event in mixed_events] == [
        "Let us start with the demo.",
        "Sounds good, sharing now.",
    ]
    assert [entry["stream_id"] for entry in mixed_entries] == ["", ""]
    assert [entry["stream_id"] for entry in stream_entries] == ["slot-0", "slot-1"]
    assert [
        {key: value for key, value in entry.items() if key not in ("id", "stream_id")}
        for entry in mixed_entries
    ] == [
        {key: value for key, value in entry.items() if key not in ("id", "stream_id")}
        for entry in stream_entries
    ]
    stream_diagnostics = ("speaker_reason", "speaker_slot", "speaker_bound", "roster_size")
    assert [{key: row[key] for key in stream_diagnostics} for row in stream_rows] == [
        {
            "speaker_reason": "no_roster",
            "speaker_slot": 0,
            "speaker_bound": False,
            "roster_size": 0,
        },
        {
            "speaker_reason": "no_roster",
            "speaker_slot": 1,
            "speaker_bound": False,
            "roster_size": 0,
        },
    ]
    assert {
        frozenset(key for key in row if key not in stream_diagnostics) for row in mixed_rows
    } == {frozenset(key for key in row if key not in stream_diagnostics) for row in stream_rows}
    assert [
        {
            key: value
            for key, value in row.items()
            if key not in ("transcript_id", "stream_id", *stream_diagnostics)
        }
        for row in mixed_rows
    ] == [
        {
            key: value
            for key, value in row.items()
            if key not in ("transcript_id", "stream_id", *stream_diagnostics)
        }
        for row in stream_rows
    ]


async def test_stream_callback_attributes_via_binder(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from fakeyou.foundation.transcript import TranscriptUpdate
    from fakeyou.observability.journal import Journal
    from fakeyou.orchestration.transcript import build_transcript_callback
    from fakeyou.persistence.record import MeetingRecord, meeting_record_path

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    binder.observe_stream(0, [111])
    binder.observe_stream(1, [222])
    frames = [
        TranscriptUpdate(
            text="Welcome to the call.",
            final=True,
            timestamp="2026-10-06T01:00:01+00:00",
        ),
        TranscriptUpdate(
            text="Thanks, glad to join.",
            final=True,
            timestamp="2026-10-06T01:00:02+00:00",
        ),
    ]
    frames[0] = replace(frames[0], stream_id="slot-0")
    frames[1] = replace(frames[1], stream_id="slot-1")

    kernel, heard = _heard_finals()
    journal = Journal("meeting-9", emit=lambda _line: None, now=lambda: now, echo=False)
    path = meeting_record_path(tmp_path, SessionId("meeting-9"))
    record = MeetingRecord(path, now=lambda: now)
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, kernel=kernel),
        binder=binder,
        voice_activity=None,
        options=DrainOptions(per_stream_enabled=True),
    )
    for frame in frames:
        await callback(frame)
    await kernel.settled()
    seen = heard.events
    record.close()
    journal.close()
    assert [event.participant_id for event in seen] == ["speaker-a", "speaker-b"]
    assert [event.speaker_evidence_id for event in seen] == ["stream-0", "stream-1"]
    assert [event.stream_id for event in seen] == ["slot-0", "slot-1"]
    entries = [
        row
        for line in path.read_text(encoding="utf-8").splitlines()
        if (row := json.loads(line))["kind"] != "header"
    ]
    assert [entry["participant_id"] for entry in entries] == ["speaker-a", "speaker-b"]
    assert [entry["stream_id"] for entry in entries] == ["slot-0", "slot-1"]


async def test_registry_feed_stamps_capture_clock_on_input() -> None:
    registry, made = recording_registry(FakeClock().monotonic)
    await registry.feed(_chunk(slot=0, seq=4, page_ms=1234.5))
    assert made[0].last_capture == (4, 1234.5)


async def test_drain_poll_propagates_unexpected_input_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    payload = {
        "streams": [
            {
                "slot": 0,
                "seq": 0,
                "csrcs": [111],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
        ],
        "roster": [],
        "dropped": 0,
    }
    page = meet_page([], evaluates=[payload])
    registry, _made = recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(page, registry)

    async def boom(_chunk: StreamChunk) -> None:
        raise AttributeError("bug")

    monkeypatch.setattr(registry, "feed", boom)
    with pytest.raises(AttributeError, match="bug"):
        await loop.poll()


async def test_registry_feed_propagates_unexpected_recorder_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.audio_persist import SlotAudioWriter

    registry, _made = recording_registry(FakeClock().monotonic)
    writer = SlotAudioWriter(tmp_path / "rec")

    def boom(_slot: int, _seq: int, _page_ms: float, _pcm: bytes) -> None:
        raise KeyError("bug")

    monkeypatch.setattr(writer, "write_chunk", boom)
    monkeypatch.setattr(registry, "recorder", writer)
    with pytest.raises(KeyError, match="bug"):
        await registry.feed(_chunk(slot=0, seq=0))


async def test_registry_feed_records_closed_writer_as_persist_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.audio_persist import SlotAudioWriter

    journal = RecordingJournal()
    registry, _made = recording_registry(FakeClock().monotonic, journal=journal)
    writer = SlotAudioWriter(tmp_path / "rec")
    writer.close()
    monkeypatch.setattr(registry, "recorder", writer)

    await registry.feed(_chunk(slot=0, seq=0))

    [row] = journal.events("fault_recorded")
    assert row["kind"] == "callback_failed"
    assert row["detail"] == "audio_persist: ValueError"
