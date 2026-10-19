from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.orchestration.transcript import (
    TranscriptDrain,
    TranscriptSinks,
    build_transcript_callback,
    record_visual_caption,
)

if TYPE_CHECKING:
    from fakeyou.conversation.turn_ingress import TurnIngress
from fakeyou.config.identifiers import SessionId
from fakeyou.persistence.record import (
    MeetingRecord,
    VisualEntry,
    load_meeting_record,
    meeting_record_path,
)
from fakeyou.visual.visual_caption import VisualCaption, frame_ref
from fakeyou.visual.visual_store import VisualFrame, VisualStore
from tests._persistence_fakes import RecordingJournal

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
        self.calls: list[tuple[str, Any]] = []

    async def receive_final(self, event: Any) -> None:
        self.events.append(event)
        self.calls.append(("transcript", event.text))

    async def handle_interim(self, text: str) -> None:
        self.calls.append(("interim", text))


INGRESS: tuple[tuple[str, Any], ...] = (
    ("vad", True),
    ("interim", "can you help"),
    ("vad", False),
    ("final", "can you help me today?"),
    ("interim", "wait"),
    ("vad", False),
)
INGRESS_CALLS: list[tuple[str, Any]] = [
    ("interim", "can you help"),
    ("transcript", "can you help me today?"),
    ("interim", "wait"),
]


def _ingress_update(text: str, *, final: bool) -> TranscriptUpdate:
    return TranscriptUpdate(text=text, final=final, timestamp=STAMPS[0])


async def _direct_ingress(tmp_path: Path) -> list[tuple[str, Any]]:
    journal = RecordingJournal("meeting-ingress")
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-ingress")))
    conversation = _Conversation()
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, cast("TurnIngress", conversation)),
        binder=SlotBinder(),
        voice_activity=None,
    )
    for kind, value in INGRESS:
        if kind == "interim":
            await conversation.handle_interim(value)
        elif kind == "final":
            await callback(_ingress_update(value, final=True))
    record.close()
    journal.close()
    return conversation.calls


async def _drained_ingress(tmp_path: Path) -> list[tuple[str, Any]]:
    journal = RecordingJournal("meeting-ingress")
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-ingress")))
    conversation = _Conversation()
    drain = TranscriptDrain(
        sinks=TranscriptSinks(journal, record, cast("TurnIngress", conversation)),
        binder=SlotBinder(),
        voice_activity=None,
    )
    for kind, value in INGRESS:
        if kind == "vad":
            await drain.on_vad(active=value)
        elif kind == "interim":
            await drain.on_interim(_ingress_update(value, final=False))
        else:
            await drain(_ingress_update(value, final=True))
    record.close()
    journal.close()
    return conversation.calls


async def test_drained_ingress_reaches_the_loop_exactly_like_direct_calls(tmp_path: Path) -> None:
    direct = await _direct_ingress(tmp_path / "direct")
    drained = await _drained_ingress(tmp_path / "drained")

    assert direct == INGRESS_CALLS
    assert drained == direct


async def test_stop_ingress_is_arbitrated_as_a_stop_event(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-stop")
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-stop")))
    drain = TranscriptDrain(
        sinks=TranscriptSinks(journal, record, None),
        binder=SlotBinder(),
        voice_activity=None,
    )

    winner = await drain.on_stop()

    assert winner is not None
    assert winner.kind == "stop"
    record.close()
    journal.close()


async def test_transcript_callback_attaches_window_refs(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-visual")
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-visual")))
    conversation = _Conversation()
    store = visual_store(tmp_path / "visual")
    sinks = TranscriptSinks(journal, record, cast("TurnIngress", conversation), visual_store=store)
    callback = build_transcript_callback(
        sinks=sinks,
        binder=SlotBinder(),
        voice_activity=None,
        per_stream_enabled=True,
    )
    frame = TranscriptUpdate(
        text="What date is shown?", final=True, timestamp="2026-10-09T03:10:05+00:00"
    )
    frame = replace(frame, stream_id="slot-0")
    frame_any = cast(Any, frame)
    frame_any = replace(frame_any, capture_ms=100000.0)

    await callback(frame)

    assert [event.visual_refs for event in conversation.events] == [("frame-launch",)]
    record.close()
    journal.close()


async def test_transcript_callback_falls_back_to_wall_clock(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-visual")
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-visual")))
    conversation = _Conversation()
    store = visual_store(tmp_path / "visual")
    sinks = TranscriptSinks(journal, record, cast("TurnIngress", conversation), visual_store=store)
    callback = build_transcript_callback(
        sinks=sinks,
        binder=SlotBinder(),
        voice_activity=None,
        per_stream_enabled=False,
    )
    frame = TranscriptUpdate(
        text="What is on screen?", final=True, timestamp="2026-10-09T03:10:11+00:00"
    )

    await callback(frame)

    assert [event.visual_refs for event in conversation.events] == [
        ("frame-launch", "frame-pricing")
    ]
    record.close()
    journal.close()


async def test_transcript_callback_skips_visual_without_store(tmp_path: Path) -> None:
    journal = RecordingJournal("meeting-visual")
    record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-visual")))
    conversation = _Conversation()
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, cast("TurnIngress", conversation)),
        binder=SlotBinder(),
        voice_activity=None,
        per_stream_enabled=True,
    )
    frame = TranscriptUpdate(
        text="What date is shown?", final=True, timestamp="2026-10-09T03:10:05+00:00"
    )
    frame = replace(frame, stream_id="slot-0")
    frame_any = cast(Any, frame)
    frame_any = replace(frame_any, capture_ms=100000.0)

    await callback(frame)

    assert [event.visual_refs for event in conversation.events] == [()]
    record.close()
    journal.close()


def test_record_visual_caption_writes_visual_entry(tmp_path: Path) -> None:
    now = datetime(2026, 10, 9, 3, 12, tzinfo=UTC)
    path = meeting_record_path(tmp_path, SessionId("meeting-visual"))
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


def test_record_visual_caption_drops_bad_stamp_with_journal_trace(tmp_path: Path) -> None:
    now = datetime(2026, 10, 9, 3, 12, tzinfo=UTC)
    path = meeting_record_path(tmp_path, SessionId("meeting-visual"))
    record = MeetingRecord(path, now=lambda: now)
    store = visual_store(tmp_path / "visual")
    frame = replace(store.frames[0], captured_at="not-a-time")
    caption = VisualCaption(
        visual_id=frame.frame_id, text="A launch timeline.", tokens=1100, cost_usd=0.0011
    )
    journal = RecordingJournal("meeting-visual")

    record_visual_caption(record, frame, caption, journal=journal)
    record.close()

    assert load_meeting_record(path) == []
    [row] = journal.events("visual_caption_dropped")
    assert row["visual_id"] == frame.frame_id
    assert row["error_type"] == "ValueError"
