import json
from datetime import UTC, datetime

from fakeyou.journal import ProviderJournal, registered_journal_events


def test_record_carries_meeting_id_timestamp_and_fields() -> None:
    lines: list[str] = []
    frozen = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    journal = ProviderJournal("meeting-1", emit=lines.append, now=lambda: frozen)

    journal.record("synth_finished", duration_ms=12.5, chars=5, bytes_out=480)

    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "synth_finished"
    assert row["meeting_id"] == "meeting-1"
    assert row["at"] == frozen.isoformat()
    assert row["duration_ms"] == 12.5
    assert row["chars"] == 5
    assert row["bytes_out"] == 480


def test_raising_emit_is_swallowed() -> None:
    def broken(line: str) -> None:
        raise OSError("disk gone")

    journal = ProviderJournal("meeting-1", emit=broken)

    journal.record("synth_started", chars=3)


def test_unregistered_event_is_recorded_never_rejected() -> None:
    lines: list[str] = []
    journal = ProviderJournal("meeting-1", emit=lines.append)

    journal.record("future_custom_event", outcome="ok")

    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "future_custom_event"
    assert "future_custom_event" in registered_journal_events()


def test_known_events_are_registered() -> None:
    registered = registered_journal_events()

    assert {
        "synth_started",
        "synth_finished",
        "synth_failed",
        "stt_reconnect",
    } <= registered


def test_record_never_raises_on_bad_fields() -> None:
    journal = ProviderJournal("meeting-1", emit=lambda line: None)

    journal.record("synth_finished", duration_ms=float("nan"))
    journal.record("", **{})


def test_request_lifecycle_has_correlation_and_elapsed(tmp_path) -> None:
    journal = ProviderJournal("meeting-1", path=tmp_path / "events.jsonl", stdout=False)

    journal.request_started("req-1", source="tts", operation="synthesize", at=1.0)
    journal.request_finished("req-1", elapsed_ms=12.5, status=200)

    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [row["event"] for row in rows] == ["request_started", "request_finished"]
    assert all(row["request_id"] == "req-1" for row in rows)
    assert rows[1]["elapsed_ms"] == 12.5


def test_journal_is_bounded_and_drops_sensitive_fields(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    journal = ProviderJournal("meeting-1", path=path, max_bytes=180, stdout=False)

    journal.record(
        "request_failed",
        request_id="req-1",
        api_key="sk-secret",
        transcript_body="private",
        detail="x" * 500,
    )

    assert path.stat().st_size <= 180
    text = path.read_text()
    assert "sk-secret" not in text
    assert "private" not in text


def test_journal_should_omit_text_content_from_every_nested_event(tmp_path):
    path = tmp_path / "events.jsonl"
    journal = ProviderJournal("meeting-1", path=path, stdout=False)
    for name in ("transcript_final", "speak_started", "speak_finished", "speak_dropped"):
        journal.record(name, text="private reply?", metadata={"text": "private transcript"})
    journal.close()

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 4
    assert all("text" not in row and "text" not in row["metadata"] for row in rows)
    assert "private" not in path.read_text()
