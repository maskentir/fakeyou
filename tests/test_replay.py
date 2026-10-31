import json
import sys
from pathlib import Path

import pytest

import fakeyou.cli.main as cli_main
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.observability.journal import journal_path
from fakeyou.persistence.record import meeting_record_path
from tests._replay_support import recorded_conversation

MEETING = SessionId("m-1")


def recorded_meeting(root: Path) -> tuple[Path, Path]:
    rows, entries = recorded_conversation()
    journal_dir, record_dir = root / "journals", root / "meetings"
    journal = journal_path(journal_dir, MEETING)
    journal.parent.mkdir(parents=True)
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    record = meeting_record_path(record_dir, MEETING)
    record.parent.mkdir(parents=True)
    record.write_text(
        "".join(entry.model_dump_json() + "\n" for entry in entries), encoding="utf-8"
    )
    (record.parent / "session.json").write_text(
        json.dumps({"twin_name": "Harun", "wake_phrase": "Harun", "barge_in": True}),
        encoding="utf-8",
    )
    return journal_dir, record_dir


def run(argv: list[str], monkeypatch: pytest.MonkeyPatch, settings: Settings) -> int:
    monkeypatch.setattr(cli_main, "_load_settings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "replay", *argv])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()
    code = exited.value.code
    return code if isinstance(code, int) else 1


def test_the_replay_command_reports_the_gate_and_writes_the_replayed_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal_dir, record_dir = recorded_meeting(tmp_path)
    out = tmp_path / "replay.jsonl"

    code = run(
        [
            "--meeting-id",
            str(MEETING),
            "--journal-dir",
            str(journal_dir),
            "--record-dir",
            str(record_dir),
            "--out",
            str(out),
        ],
        monkeypatch,
        settings,
    )

    report = json.loads(capsys.readouterr().out)
    assert code == ExitCode.SUCCESS
    assert report["meeting_id"] == str(MEETING)
    assert report["passed"] is True
    assert {check["name"] for check in report["gated"]} >= {
        "vad_stop_to_final_ms.p50",
        "final_to_first_pcm_ms.p50",
        "speaker_bound.of",
    }
    assert all(check["reason"] for check in report["reported"])
    replayed = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert any(row["event"] == "synthesis_pcm_emitted" for row in replayed)


def test_a_replay_that_misses_the_gate_exits_as_a_failed_eval_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal_dir, record_dir = recorded_meeting(tmp_path)
    journal = journal_path(journal_dir, MEETING)
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        if row["event"] == "synthesis_pcm_emitted":
            row["at"] = row["at"].replace("15:00:03.400000", "15:00:05.400000")
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    code = run(
        [
            "--meeting-id",
            str(MEETING),
            "--journal-dir",
            str(journal_dir),
            "--record-dir",
            str(record_dir),
        ],
        monkeypatch,
        settings,
    )

    report = json.loads(capsys.readouterr().out)
    assert code == ExitCode.EVAL_GATE_FAILED
    assert report["passed"] is False


def test_halving_the_recorded_reply_latency_misses_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    journal_dir, record_dir = recorded_meeting(tmp_path)

    code = run(
        [
            "--meeting-id",
            str(MEETING),
            "--journal-dir",
            str(journal_dir),
            "--record-dir",
            str(record_dir),
            "--reply-latency-scale",
            "0.5",
        ],
        monkeypatch,
        settings,
    )

    report = json.loads(capsys.readouterr().out)
    assert code == ExitCode.EVAL_GATE_FAILED
    assert report["passed"] is False


@pytest.mark.parametrize("scale", ["-1", "nan", "inf"])
def test_a_reply_latency_scale_that_is_not_a_finite_non_negative_number_is_rejected(
    scale: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    journal_dir, record_dir = recorded_meeting(tmp_path)

    code = run(
        [
            "--meeting-id",
            str(MEETING),
            "--journal-dir",
            str(journal_dir),
            "--record-dir",
            str(record_dir),
            "--reply-latency-scale",
            scale,
        ],
        monkeypatch,
        settings,
    )

    assert code == 2
