from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pipecat.frames.frames import TranscriptionFrame

from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.cli.join import TranscriptSinks, build_transcript_callback, record_visual_caption

if TYPE_CHECKING:
    from fakeyou.conversation.conversation import ConversationLoop
from fakeyou.persistence.record import (
    MeetingRecord,
    VisualEntry,
    load_meeting_record,
    meeting_record_path,
)
from fakeyou.visual.visual_caption import VisualCaption, frame_ref
from fakeyou.visual.visual_store import VisualFrame, VisualStore
from tests.fakes import RecordingJournal

FRAME_IDS = ["frame-launch", "frame-pricing", "frame-roadmap"]
PAGE_MS = [100000.0, 110000.0, 130000.0]
STAMPS = [
    "2026-10-09T03:10:00+00:00",
    "2026-10-09T03:10:10+00:00",
    "2026-10-09T03:10:30+00:00",
]
DHASHES = [0x0000000000000000, 0xFFFFFFFFFFFFFFFF, 0xAAAAAAAAAAAAAAAA]


def visual_store(directory: Path) -> VisualStore:
    store = VisualStore(directory)
    for index, frame_id in enumerate(FRAME_IDS):
        shot = directory / f"{frame_id}.jpg"
        thumb = directory / f"{frame_id}.thumb.jpg"
        shot.write_bytes(b"frame-bytes-" + frame_id.encode())
        thumb.write_bytes(b"thumb-bytes-" + frame_id.encode())
        store.append(
            VisualFrame(
                frame_id=frame_id,
                captured_page_ms=PAGE_MS[index],
                captured_at=STAMPS[index],
                sha256=f"{index:02x}" * 32,
                path=str(shot),
                thumb_path=str(thumb),
                dhash=DHASHES[index],
            )
        )
    assert len(store.frames) == 3
    return store


class _Conversation:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def handle_transcript(self, event: Any) -> None:
        self.events.append(event)


async def test_transcript_callback_attaches_window_refs(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-visual")
    record = MeetingRecord(meeting_record_path(tmp_path, "meeting-visual"))
    conversation = _Conversation()
    store = visual_store(tmp_path / "visual")
    sinks = TranscriptSinks(
        journal, record, cast("ConversationLoop", conversation), visual_store=store
    )
    callback = build_transcript_callback(
        sinks=sinks,
        binder=SlotBinder(),
        voice_activity=None,
        per_stream_enabled=True,
    )
    frame = TranscriptionFrame(
        "What date is shown?", "", "2026-10-09T03:10:05+00:00", finalized=True
    )
    frame.transport_source = "slot-0"
    frame_any = cast(Any, frame)
    frame_any.capture_ms = 100000.0

    await callback(frame)

    assert [event.visual_refs for event in conversation.events] == [("frame-launch",)]
    record.close()
    journal.close()


async def test_transcript_callback_falls_back_to_wall_clock(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-visual")
    record = MeetingRecord(meeting_record_path(tmp_path, "meeting-visual"))
    conversation = _Conversation()
    store = visual_store(tmp_path / "visual")
    sinks = TranscriptSinks(
        journal, record, cast("ConversationLoop", conversation), visual_store=store
    )
    callback = build_transcript_callback(
        sinks=sinks,
        binder=SlotBinder(),
        voice_activity=None,
        per_stream_enabled=False,
    )
    frame = TranscriptionFrame(
        "What is on screen?", "", "2026-10-09T03:10:11+00:00", finalized=True
    )

    await callback(frame)

    assert [event.visual_refs for event in conversation.events] == [
        ("frame-launch", "frame-pricing")
    ]
    record.close()
    journal.close()


async def test_transcript_callback_skips_visual_without_store(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-visual")
    record = MeetingRecord(meeting_record_path(tmp_path, "meeting-visual"))
    conversation = _Conversation()
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, cast("ConversationLoop", conversation)),
        binder=SlotBinder(),
        voice_activity=None,
        per_stream_enabled=True,
    )
    frame = TranscriptionFrame(
        "What date is shown?", "", "2026-10-09T03:10:05+00:00", finalized=True
    )
    frame.transport_source = "slot-0"
    frame_any = cast(Any, frame)
    frame_any.capture_ms = 100000.0

    await callback(frame)

    assert [event.visual_refs for event in conversation.events] == [()]
    record.close()
    journal.close()


def test_record_visual_caption_writes_visual_entry(tmp_path: Path) -> None:
    now = datetime(2026, 10, 9, 3, 12, tzinfo=UTC)
    path = meeting_record_path(tmp_path, "meeting-visual")
    record = MeetingRecord(path, now=lambda: now)
    store = visual_store(tmp_path / "visual")
    frame = store.frames[0]
    caption = VisualCaption(
        visual_id=frame.frame_id, text="A launch timeline.", tokens=1100, cost_usd=0.0011
    )

    record_visual_caption(record, frame, caption)
    record.close()

    [entry] = load_meeting_record(path)
    assert isinstance(entry, VisualEntry)
    assert entry.visual_id == "frame-launch"
    assert entry.caption == "A launch timeline."
    assert entry.sha256 == frame.sha256
    assert entry.frame_ref == frame_ref(frame)
    assert entry.captured_page_ms == 100000.0
