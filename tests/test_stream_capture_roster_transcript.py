import asyncio
import json
from pathlib import Path
from typing import Any

from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.config.identifiers import SessionId
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.transcript import TranscriptSinks


async def test_stream_callback_surfaces_refusal_reason_and_roster_size(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.audio.voice_activity import SlotBinder
    from fakeyou.orchestration.transcript import build_transcript_callback
    from fakeyou.persistence.record import MeetingRecord, meeting_record_path

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, now=lambda: now, echo=False)
    path = meeting_record_path(tmp_path, SessionId("meeting-9"))
    record = MeetingRecord(path, now=lambda: now)
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None),
        binder=SlotBinder(),
        voice_activity=None,
        per_stream_enabled=True,
    )
    frame = TranscriptionFrame(
        "Hello from the call.", "", "2026-10-06T01:00:01+00:00", finalized=True
    )
    frame.transport_source = "slot-0"
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

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.audio.voice_activity import SlotBinder
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
        sinks=TranscriptSinks(journal, record, None),
        binder=binder,
        voice_activity=None,
        per_stream_enabled=True,
    )
    frame = TranscriptionFrame(
        "Welcome to the call.", "", "2026-10-06T01:00:01+00:00", finalized=True
    )
    frame.transport_source = "slot-0"
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

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.audio.voice_activity import SlotBinder
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
        sinks=TranscriptSinks(journal, record, None),
        binder=binder,
        voice_activity=None,
        per_stream_enabled=True,
    )
    first = TranscriptionFrame(
        "Welcome to the call.", "", "2026-10-06T01:00:01+00:00", finalized=True
    )
    first.transport_source = "slot-0"
    await callback(first)
    binder.observe_stream(1, [111])
    second = TranscriptionFrame(
        "Still here on the call.", "", "2026-10-06T01:00:02+00:00", finalized=True
    )
    second.transport_source = "slot-1"
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

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.audio.voice_activity import SlotBinder
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
        sinks=TranscriptSinks(journal, record, None),
        binder=binder,
        voice_activity=None,
        per_stream_enabled=True,
    )
    later = TranscriptionFrame(
        "Second sentence spoken first.", "", "2026-10-06T01:00:02+00:00", finalized=True
    )
    later.transport_source = "slot-0"
    await callback(later)
    earlier = TranscriptionFrame(
        "First sentence arriving late.", "", "2026-10-06T01:00:01+00:00", finalized=True
    )
    earlier.transport_source = "slot-1"
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

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.audio.voice_activity import SlotBinder
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
        sinks=TranscriptSinks(journal, record, None),
        binder=binder,
        voice_activity=None,
        per_stream_enabled=True,
    )
    for stamp, slot, text in [
        ("2026-10-06T01:00:01+00:00", "slot-0", "First."),
        ("2026-10-06T01:00:01+00:00", "slot-1", "Same instant."),
        ("2026-10-06T01:00:02+00:00", "slot-0", "Second."),
    ]:
        frame = TranscriptionFrame(text, "", stamp, finalized=True)
        frame.transport_source = slot
        await callback(frame)
    record.close()
    journal.close()
    rows = [json.loads(line) for line in lines]
    assert len([row for row in rows if row["event"] == "transcript_finalized"]) == 3
    assert [row for row in rows if row["event"] == "order_inversion_detected"] == []


async def test_tagged_callback_stamps_capture_clock() -> None:
    from pipecat.frames.frames import TranscriptionFrame
    from pipecat.transports.base_transport import TransportParams

    from fakeyou.audio.audio import (
        StreamCallbacks,
        StreamComponents,
        StreamSource,
        open_stream_audio,
    )
    from fakeyou.audio.stream_capture import StreamAudioInput
    from tests._capture_support import EchoSTT, FakePlayback

    async def transcribe(streamer: StreamAudioInput) -> Any:
        seen: list[Any] = []
        ready = asyncio.Event()

        async def record(frame: TranscriptionFrame) -> None:
            seen.append(frame)
            ready.set()

        async with open_stream_audio(
            source=StreamSource("slot-0", streamer),
            callbacks=StreamCallbacks(record),
            components=StreamComponents(stt=EchoSTT("hi"), playback=FakePlayback()),
        ):
            await streamer.feed(b"\x01\x02" * 160)
            await asyncio.wait_for(ready.wait(), timeout=5)
        (frame,) = seen
        return frame

    stamped = StreamAudioInput(TransportParams(audio_in_enabled=True))
    stamped.note_capture(7, 99.5)
    frame = await transcribe(stamped)
    assert (frame.capture_seq, frame.capture_ms) == (7, 99.5)
    plain = await transcribe(StreamAudioInput(TransportParams(audio_in_enabled=True)))
    assert getattr(plain, "capture_ms", 0.0) == 0.0


def test_attribute_stream_frame_returns_capture_ms() -> None:
    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.orchestration.transcript import attribute_stream_frame

    stamped = TranscriptionFrame("hi", "", "2026-10-06T01:00:01+00:00")
    stamped.transport_source = "slot-2"
    stamped_any: Any = stamped
    stamped_any.capture_ms = 55.5
    assert attribute_stream_frame(stamped, SlotBinder(capacity=4))[4] == 55.5
    plain = TranscriptionFrame("hi", "", "2026-10-06T01:00:01+00:00")
    plain.transport_source = "slot-2"
    assert attribute_stream_frame(plain, SlotBinder(capacity=4))[4] == 0.0
