import json
from datetime import UTC, datetime

from fakeyou.journal import Journal


def test_record_carries_meeting_id_timestamp_and_fields() -> None:
    lines: list[str] = []
    frozen = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    journal = Journal("meeting-1", emit=lines.append, now=lambda: frozen)

    journal.record("synthesis_finished", source="tts", duration_ms=12.5, chars=5, bytes_out=480)

    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "synthesis_finished"
    assert row["meeting_id"] == "meeting-1"
    assert row["at"] == frozen.isoformat()
    assert row["duration_ms"] == 12.5
    assert row["chars"] == 5
    assert row["bytes_out"] == 480


def test_raising_emit_is_swallowed() -> None:
    def broken(line: str) -> None:
        raise OSError("disk gone")

    journal = Journal("meeting-1", emit=broken)

    journal.record("synthesis_started", source="tts", chars=3)


def test_unregistered_event_is_recorded_never_rejected() -> None:
    lines: list[str] = []
    journal = Journal("meeting-1", emit=lines.append)

    journal.record("future_custom_event", source="runtime", outcome="ok")

    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "future_custom_event"


def test_record_never_raises_on_bad_fields() -> None:
    journal = Journal("meeting-1", emit=lambda line: None)

    journal.record("synthesis_finished", source="tts", duration_ms=float("nan"))
    journal.record("", source="runtime")


def test_request_lifecycle_has_correlation_and_elapsed(tmp_path) -> None:
    journal = Journal("meeting-1", path=tmp_path / "events.jsonl", echo=False)

    journal.request_started("req-1", source="tts", operation="synthesize", at=1.0)
    journal.request_finished("req-1", duration_ms=12.5, status=200)

    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [row["event"] for row in rows] == ["request_started", "request_finished"]
    assert all(row["request_id"] == "req-1" for row in rows)
    assert rows[1]["duration_ms"] == 12.5


def test_journal_is_bounded_and_drops_sensitive_fields(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    journal = Journal("meeting-1", path=path, max_bytes=180, echo=False)

    journal.record(
        "request_failed",
        source="llm",
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
    journal = Journal("meeting-1", path=path, echo=False)
    for name in (
        "transcript_finalized",
        "playback_started",
        "playback_finished",
        "playback_dropped",
    ):
        journal.record(
            name, source="playback", text="private reply?", metadata={"text": "private transcript"}
        )
    journal.close()

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 4
    assert all("text" not in row and "text" not in row["metadata"] for row in rows)
    assert "private" not in path.read_text()


def test_timed_should_correlate_success_and_preserve_result_fields(monkeypatch):
    import fakeyou.journal as module

    lines = []
    journal = Journal("timed", emit=lines.append)
    ticks = iter([1.0, 1.25])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    with journal.timed(
        source="tts",
        started="synthesis_started",
        finished="synthesis_finished",
        failed="synthesis_failed",
        chars=4,
    ) as timing:
        timing.fields["bytes_out"] = 128
    rows = [json.loads(line) for line in lines]
    assert [row["event"] for row in rows] == ["synthesis_started", "synthesis_finished"]
    assert rows[0]["request_id"] == rows[1]["request_id"]
    assert rows[1]["duration_ms"] == 250
    assert rows[1]["bytes_out"] == 128


def test_timed_should_record_failure_and_propagate_original_error():
    import pytest

    from fakeyou.provider_faults import ProviderFault

    lines = []
    journal = Journal("timed", emit=lines.append)
    error = ProviderFault("tts", "timeout")
    with pytest.raises(ProviderFault) as raised:
        with journal.timed(
            started="synthesis_started",
            finished="synthesis_finished",
            failed="synthesis_failed",
            source="tts",
        ):
            raise error
    assert raised.value is error
    rows = [json.loads(line) for line in lines]
    assert [row["event"] for row in rows] == ["synthesis_started", "synthesis_failed"]
    assert rows[-1]["kind"] == "timeout"
    assert rows[-1]["duration_ms"] >= 0


def test_meet_action_lifecycle_shares_one_action_id() -> None:
    lines: list[str] = []
    journal = Journal("meeting-1", emit=lines.append)

    journal.meet_action_requested(action="raise_hand", action_id="a1", actor="twin", attempt=1)
    journal.meet_action_started(action="raise_hand", action_id="a1", attempt=1)
    journal.meet_action_observed(action="raise_hand", action_id="a1", observed_state="hand_raised")
    journal.meet_action_finished(
        action="raise_hand", action_id="a1", observed=True, attempts=1, duration_ms=1200
    )
    journal.meet_action_requested(action="send_chat", action_id="c1", actor="twin", attempt=1)
    journal.meet_action_failed(
        action="send_chat", action_id="c1", reason="timeout", attempts=2, duration_ms=900
    )
    journal.meet_action_requested(action="lower_hand", action_id="a2", actor="twin", attempt=1)
    journal.meet_action_rejected(action="lower_hand", action_id="a2", reason="authority_denied")

    rows = [json.loads(line) for line in lines]
    assert [row["event"] for row in rows] == [
        "meet_action_requested",
        "meet_action_started",
        "meet_action_observed",
        "meet_action_finished",
        "meet_action_requested",
        "meet_action_failed",
        "meet_action_requested",
        "meet_action_rejected",
    ]
    assert [row["action_id"] for row in rows] == ["a1", "a1", "a1", "a1", "c1", "c1", "a2", "a2"]
    assert all(row["source"] == "agent" for row in rows)
    assert rows[0]["actor"] == "twin"
    assert rows[3]["duration_ms"] == 1200
    assert rows[5]["reason"] == "timeout"
    assert rows[7]["reason"] == "authority_denied"


def test_meet_action_events_carry_chars_without_chat_content() -> None:
    lines: list[str] = []
    journal = Journal("meeting-1", emit=lines.append)

    journal.meet_action_requested(
        action="send_chat",
        action_id="c1",
        actor="twin",
        attempt=1,
        chars=42,
        text="secret hello",
    )
    journal.meet_action_observed(
        action="send_chat",
        action_id="c1",
        observed_state="chat_echoed",
        chars=42,
        body="secret echo",
    )

    rows = [json.loads(line) for line in lines]
    assert [row["chars"] for row in rows] == [42, 42]
    assert all("text" not in row for row in rows)
    assert "secret" not in "\n".join(lines)
