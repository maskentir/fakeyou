import json
from datetime import UTC, datetime
from pathlib import Path

from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.config.identifiers import SessionId
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.transcript import TranscriptSinks, build_transcript_callback
from fakeyou.persistence.record import MeetingRecord, meeting_record_path
from tests._fakes_common import FakeClock
from tests._voice_support import batch, speak, tracker_at

REQUEST = "Rani Twin, please raise your hand."


async def test_a_declined_early_commit_leaves_the_speaker_for_the_final(tmp_path: Path) -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speak(tracker, clock, 10.2, 11.2)
    clock.now = 11.4
    tracker.observe_batch(
        batch(1000.0 + 11_400.0, [[1000.0 + 10_050.0, 3, 0.3]]),
        sent_ms=11_400.0,
        received_ms=11_400.0,
    )
    now = datetime(2026, 11, 8, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-early", emit=lines.append, now=lambda: now, echo=False)
    record = MeetingRecord(
        meeting_record_path(tmp_path, SessionId("meeting-early")), now=lambda: now
    )
    drain = build_transcript_callback(
        sinks=TranscriptSinks(journal, record), binder=SlotBinder(), voice_activity=tracker
    )

    early = drain.early_event(REQUEST)
    await drain(TranscriptUpdate(text=REQUEST, final=True, timestamp=now.isoformat()))
    record.close()
    journal.close()

    [final] = [
        row
        for row in map(json.loads, lines)
        if row["event"] == "transcript_finalized" and row.get("text", REQUEST) == REQUEST
    ]
    assert early.participant_id == "speaker-a"
    assert final["participant_id"] == "speaker-a"
    assert final["speaker_reason"] == "dominant_slot"
