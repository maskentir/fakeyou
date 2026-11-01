import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.config.identifiers import SessionId
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.transcript import DrainOptions, TranscriptSinks


async def test_stream_callback_surfaces_refusal_reason_and_roster_size(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from fakeyou.audio.voice_activity import SlotBinder
    from fakeyou.foundation.transcript import TranscriptUpdate
    from fakeyou.orchestration.transcript import build_transcript_callback
    from fakeyou.persistence.record import MeetingRecord, meeting_record_path

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, now=lambda: now, echo=False)
    path = meeting_record_path(tmp_path, SessionId("meeting-9"))
    record = MeetingRecord(path, now=lambda: now)
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record),
        binder=SlotBinder(),
        voice_activity=None,
        options=DrainOptions(per_stream_enabled=True),
    )
    frame = TranscriptUpdate(
        text="Hello from the call.", final=True, timestamp="2026-10-06T01:00:01+00:00"
    )
    frame = replace(frame, stream_id="slot-0")
    await callback(frame)
    record.close()
    journal.close()
    rows = [json.loads(line) for line in lines]
    rows = [row for row in rows if row["event"] == "transcript_finalized"]
    assert len(rows) == 1
    assert rows[0]["participant_id"] == "unknown"
    assert rows[0]["speaker_reason"] == "no_roster"
    assert rows[0]["speaker_slot"] == 0
    assert rows[0]["roster_size"] == 0


async def test_stream_callback_surfaces_bound_reason_and_roster_size(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from fakeyou.audio.voice_activity import SlotBinder
    from fakeyou.foundation.transcript import TranscriptUpdate
    from fakeyou.orchestration.transcript import build_transcript_callback
    from fakeyou.persistence.record import MeetingRecord, meeting_record_path

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, now=lambda: now, echo=False)
    path = meeting_record_path(tmp_path, SessionId("meeting-9"))
    record = MeetingRecord(path, now=lambda: now)
    binder = SlotBinder()
    binder.update_roster(["host"])
    binder.observe_stream(0, [111])
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record),
        binder=binder,
        voice_activity=None,
        options=DrainOptions(per_stream_enabled=True),
    )
    frame = TranscriptUpdate(
        text="Welcome to the call.", final=True, timestamp="2026-10-06T01:00:01+00:00"
    )
    frame = replace(frame, stream_id="slot-0")
    await callback(frame)
    record.close()
    journal.close()
    rows = [json.loads(line) for line in lines]
    rows = [row for row in rows if row["event"] == "transcript_finalized"]
    assert len(rows) == 1
    assert rows[0]["participant_id"] == "speaker-a"
    assert rows[0]["speaker_reason"] == "stream_slot"
    assert rows[0]["speaker_slot"] == 0
    assert rows[0]["speaker_bound"] is True
    assert rows[0]["roster_size"] == 1


async def test_stream_callback_journals_csrc_and_tenure_events(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from fakeyou.audio.voice_activity import SlotBinder
    from fakeyou.foundation.transcript import TranscriptUpdate
    from fakeyou.orchestration.transcript import build_transcript_callback
    from fakeyou.persistence.record import MeetingRecord, meeting_record_path

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, now=lambda: now, echo=False)
    path = meeting_record_path(tmp_path, SessionId("meeting-9"))
    record = MeetingRecord(path, now=lambda: now)
    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    binder.observe_stream(0, [111])
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record),
        binder=binder,
        voice_activity=None,
        options=DrainOptions(per_stream_enabled=True),
    )
    first = TranscriptUpdate(
        text="Welcome to the call.", final=True, timestamp="2026-10-06T01:00:01+00:00"
    )
    first = replace(first, stream_id="slot-0")
    await callback(first)
    binder.observe_stream(1, [111])
    second = TranscriptUpdate(
        text="Still here on the call.",
        final=True,
        timestamp="2026-10-06T01:00:02+00:00",
    )
    second = replace(second, stream_id="slot-1")
    await callback(second)
    record.close()
    journal.close()
    rows = [json.loads(line) for line in lines]
    rows = [row for row in rows if row["event"] == "transcript_finalized"]
    assert len(rows) == 2
    assert rows[0]["speaker_csrc"] == [111]
    assert rows[0]["tenure_events"] == [{"event": "tenure_minted", "label": "A", "slot": 0}]
    assert rows[1]["tenure_events"] == [
        {"event": "tenure_moved", "label": "A", "from_slot": 0, "to_slot": 1, "csrcs": [111]}
    ]


async def test_stream_callback_journals_late_arrival_inversion(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from fakeyou.audio.voice_activity import SlotBinder
    from fakeyou.foundation.transcript import TranscriptUpdate
    from fakeyou.orchestration.transcript import build_transcript_callback
    from fakeyou.persistence.record import MeetingRecord, meeting_record_path

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, now=lambda: now, echo=False)
    path = meeting_record_path(tmp_path, SessionId("meeting-9"))
    record = MeetingRecord(path, now=lambda: now)
    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record),
        binder=binder,
        voice_activity=None,
        options=DrainOptions(per_stream_enabled=True),
    )
    later = TranscriptUpdate(
        text="Second sentence spoken first.",
        final=True,
        timestamp="2026-10-06T01:00:02+00:00",
    )
    later = replace(later, stream_id="slot-0")
    await callback(later)
    earlier = TranscriptUpdate(
        text="First sentence arriving late.",
        final=True,
        timestamp="2026-10-06T01:00:01+00:00",
    )
    earlier = replace(earlier, stream_id="slot-1")
    await callback(earlier)
    record.close()
    journal.close()
    rows = [json.loads(line) for line in lines]
    finals = [row for row in rows if row["event"] == "transcript_finalized"]
    assert [row["spoken_at"] for row in finals] == [
        "2026-10-06T01:00:02+00:00",
        "2026-10-06T01:00:01+00:00",
    ]
    inversions = [row for row in rows if row["event"] == "order_inversion_detected"]
    assert len(inversions) == 1
    assert inversions[0]["transcript_id"] == finals[1]["transcript_id"]
    assert inversions[0]["spoken_at"] == "2026-10-06T01:00:01+00:00"
    assert inversions[0]["emitted_max_spoken_at"] == "2026-10-06T01:00:02+00:00"


async def test_stream_callback_skips_inversion_for_ordered_arrivals(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from fakeyou.audio.voice_activity import SlotBinder
    from fakeyou.foundation.transcript import TranscriptUpdate
    from fakeyou.orchestration.transcript import build_transcript_callback
    from fakeyou.persistence.record import MeetingRecord, meeting_record_path

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, now=lambda: now, echo=False)
    path = meeting_record_path(tmp_path, SessionId("meeting-9"))
    record = MeetingRecord(path, now=lambda: now)
    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record),
        binder=binder,
        voice_activity=None,
        options=DrainOptions(per_stream_enabled=True),
    )
    for stamp, slot, text in [
        ("2026-10-06T01:00:01+00:00", "slot-0", "First."),
        ("2026-10-06T01:00:01+00:00", "slot-1", "Same instant."),
        ("2026-10-06T01:00:02+00:00", "slot-0", "Second."),
    ]:
        frame = TranscriptUpdate(text=text, final=True, timestamp=stamp)
        frame = replace(frame, stream_id=slot)
        await callback(frame)
    record.close()
    journal.close()
    rows = [json.loads(line) for line in lines]
    assert len([row for row in rows if row["event"] == "transcript_finalized"]) == 3
    assert [row for row in rows if row["event"] == "order_inversion_detected"] == []


async def test_tagged_callback_stamps_capture_clock() -> None:

    from fakeyou.audio.audio import (
        StreamCallbacks,
        StreamComponents,
        StreamSource,
        open_stream_audio,
    )
    from fakeyou.audio.stream_input import StreamAudioInput
    from fakeyou.foundation.transcript import TranscriptUpdate
    from tests._capture_support import FakePlayback, echo_stt

    async def transcribe(streamer: StreamAudioInput) -> Any:
        seen: list[Any] = []
        ready = asyncio.Event()

        async def record(frame: TranscriptUpdate) -> None:
            seen.append(frame)
            ready.set()

        async with open_stream_audio(
            source=StreamSource("slot-0", streamer),
            callbacks=StreamCallbacks(record),
            components=StreamComponents(stt=echo_stt("hi"), playback=FakePlayback()),
        ):
            await streamer.feed(b"\x01\x02" * 160)
            await asyncio.wait_for(ready.wait(), timeout=5)
        (frame,) = seen
        return frame

    stamped = StreamAudioInput()
    stamped.note_capture(7, 99.5)
    frame = await transcribe(stamped)
    assert (frame.capture_seq, frame.capture_ms) == (7, 99.5)
    plain = await transcribe(StreamAudioInput())
    assert plain.capture_ms is None


def test_attribute_stream_frame_returns_capture_ms() -> None:
    from fakeyou.foundation.transcript import TranscriptUpdate
    from fakeyou.orchestration.transcript import attribute_stream_frame

    stamped = TranscriptUpdate(text="hi", final=False, timestamp="2026-10-06T01:00:01+00:00")
    stamped = replace(stamped, stream_id="slot-2")
    stamped = replace(stamped, capture_ms=55.5)
    assert attribute_stream_frame(stamped, SlotBinder(capacity=4))[4] == 55.5
    plain = TranscriptUpdate(text="hi", final=False, timestamp="2026-10-06T01:00:01+00:00")
    plain = replace(plain, stream_id="slot-2")
    assert attribute_stream_frame(plain, SlotBinder(capacity=4))[4] == 0.0
