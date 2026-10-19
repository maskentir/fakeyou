from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any

import pytest

import fakeyou.cli.main as cli_main
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.calibrate import calibrate_eval, calibrate_lock
from fakeyou.identity.verify import load_calibration_artifact
from tests._calibration_support import (
    dev_trials,
    lock_dev,
    pass_trials,
    trial_row,
    write_journal,
)
from tests._error_support import message_pattern


def test_lock_bimodal_writes_artifact_with_theta_in_valley(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    trials = dev_trials()
    out = str(tmp_path / "artifact.json")
    calibrate_lock(
        [
            write_journal(tmp_path / "dev-a.jsonl", trials[:12]),
            write_journal(tmp_path / "dev-b.jsonl", trials[12:]),
        ],
        out,
    )

    assert Path(out).is_file()
    payload = json.loads(Path(out).read_text())
    assert sorted(payload) == [
        "dev_speaker_ids",
        "margin_floor",
        "platt_a",
        "platt_b",
        "platt_version",
        "report_id",
        "snorm_cohort",
        "theta_llr",
    ]
    assert payload["dev_speaker_ids"] == sorted(payload["dev_speaker_ids"])
    assert len(payload["dev_speaker_ids"]) == 24
    assert "dev-t00" in payload["dev_speaker_ids"]
    assert not Path(out + ".speakers.json").exists()
    artifact = load_calibration_artifact(out)
    assert artifact is not None
    assert artifact.platt_version == "platt-v1"
    assert artifact.report_id.startswith("cal-")
    mean = statistics.mean(artifact.snorm_cohort)
    std = statistics.pstdev(artifact.snorm_cohort)
    llrs = [
        artifact.platt_a * (trial["s1"] - mean) / std + artifact.platt_b for trial in dev_trials()
    ]
    target_llrs = llrs[:12]
    nontarget_llrs = llrs[12:]
    assert min(target_llrs) > artifact.theta_llr > max(nontarget_llrs)
    assert 0.15 < artifact.margin_floor < 0.76
    summary = json.loads(capsys.readouterr().out)
    assert summary["theta_llr"] == artifact.theta_llr
    assert summary["report_id"] == artifact.report_id


def test_lock_ignores_transcript_text(tmp_path: Path) -> None:
    plain = tmp_path / "plain.jsonl"
    tagged = tmp_path / "tagged.jsonl"
    trials = dev_trials()
    tagged_trials = [dict(trial, transcript="hello there") for trial in trials]
    plain_out = str(tmp_path / "plain.json")
    tagged_out = str(tmp_path / "tagged.json")
    calibrate_lock([write_journal(plain, trials)], plain_out)
    calibrate_lock([write_journal(tagged, tagged_trials)], tagged_out)

    assert Path(tagged_out).read_text() == Path(plain_out).read_text()


def test_lock_unimodal_exits_5_without_artifact(tmp_path: Path) -> None:
    journal = tmp_path / "flat.jsonl"
    trials = [trial_row(f"flat-t{i:02d}", "target", 0.50 + 0.005 * i, 0.45) for i in range(10)]
    trials += [trial_row(f"flat-n{i:02d}", "nontarget", 0.50 + 0.005 * i, 0.45) for i in range(10)]
    out = str(tmp_path / "artifact.json")

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.CALIBRATE_THETA_OUTSIDE_VALLEY)
    ) as failure:
        calibrate_lock([write_journal(journal, trials)], out)

    assert failure.value.code is ErrorCode.CALIBRATE_THETA_OUTSIDE_VALLEY
    assert failure.value.exit_code is ExitCode.CALIBRATION_REJECTED
    assert not Path(out).exists()
    assert not Path(out + ".speakers.json").exists()


def test_eval_disjoint_pass_prints_metrics_exit_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = lock_dev(tmp_path)
    capsys.readouterr()
    journal = tmp_path / "eval.jsonl"

    calibrate_eval([write_journal(journal, pass_trials())], out)

    metrics = json.loads(capsys.readouterr().out)
    assert metrics["pass"] is True
    assert metrics["precision"] == 1.0
    assert metrics["wilson_lower"] >= 0.98
    assert metrics["twin_false_verify"] == 0
    assert metrics["overlap_unverified_rate"] == 1.0
    assert metrics["valley"] is True
    assert metrics["n_verified"] == 190


def test_lock_unwritable_out_exits_1(tmp_path: Path) -> None:
    journal = tmp_path / "dev.jsonl"
    out = str(tmp_path / "missing-dir" / "artifact.json")

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.CALIBRATE_ARTIFACT_NOT_WRITTEN)
    ) as failure:
        calibrate_lock([write_journal(journal, dev_trials())], out)

    assert failure.value.code is ErrorCode.CALIBRATE_ARTIFACT_NOT_WRITTEN
    assert failure.value.exit_code is ExitCode.FAILURE
    assert str(failure.value) == f"artifact cannot be written: {out}"
    assert not Path(out).exists()


def test_eval_missing_claim_agree_defaults_to_unverified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = lock_dev(tmp_path)
    capsys.readouterr()
    journal = tmp_path / "eval.jsonl"
    trials = [
        {key: value for key, value in trial.items() if key != "claim_agree"}
        for trial in pass_trials()
    ]

    with pytest.raises(SystemExit) as failure:
        calibrate_eval([write_journal(journal, trials)], out)

    assert failure.value.code == 4
    metrics = json.loads(capsys.readouterr().out)
    assert metrics["pass"] is False
    assert metrics["n_verified"] == 0


def test_eval_zero_overlap_trials_fails_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = lock_dev(tmp_path)
    capsys.readouterr()
    journal = tmp_path / "eval.jsonl"
    trials = [trial for trial in pass_trials() if not trial["overlap"]]
    assert trials

    with pytest.raises(SystemExit) as failure:
        calibrate_eval([write_journal(journal, trials)], out)

    assert failure.value.code == 4
    metrics = json.loads(capsys.readouterr().out)
    assert metrics["pass"] is False
    assert metrics["overlap_unverified_rate"] == 0.0


def test_eval_small_n_exits_4(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = lock_dev(tmp_path)
    capsys.readouterr()
    journal = tmp_path / "eval.jsonl"
    trials = [trial_row(f"eval-t{i:02d}", "target", 0.87 + 0.005 * i, 0.10) for i in range(12)]
    trials += [trial_row(f"eval-n{i:02d}", "nontarget", 0.07 + 0.005 * i, 0.02) for i in range(8)]
    trials += [
        trial_row("eval-o01", "target", 0.95, 0.10, overlap=True),
        trial_row("eval-o02", "target", 0.95, 0.10, overlap=True),
        trial_row("eval-w01", "nontarget", 0.25, 0.12, twin=True),
        trial_row("eval-w02", "nontarget", 0.25, 0.12, twin=True),
    ]

    with pytest.raises(SystemExit) as failure:
        calibrate_eval([write_journal(journal, trials)], out)

    assert failure.value.code == 4
    metrics = json.loads(capsys.readouterr().out)
    assert metrics["pass"] is False
    assert metrics["n_verified"] == 12
    assert metrics["wilson_lower"] < 0.98


def test_eval_overlapping_speaker_exits_2(tmp_path: Path) -> None:
    out = lock_dev(tmp_path)
    journal = tmp_path / "eval.jsonl"
    trials = [
        trial_row("dev-t00", "target", 0.90, 0.10),
        trial_row("eval-t01", "target", 0.90, 0.10),
    ]

    with pytest.raises(SystemExit) as failure:
        calibrate_eval([write_journal(journal, trials)], out)

    assert failure.value.code == 2


def test_eval_missing_dev_speaker_ids_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = lock_dev(tmp_path)
    stripped = str(tmp_path / "stripped.json")
    payload = json.loads(Path(out).read_text())
    del payload["dev_speaker_ids"]
    Path(stripped).write_text(json.dumps(payload))
    assert load_calibration_artifact(stripped) is not None
    capsys.readouterr()
    journal = tmp_path / "eval.jsonl"

    with pytest.raises(SystemExit) as failure:
        calibrate_eval([write_journal(journal, pass_trials())], stripped)

    assert failure.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "missing_dev_speakers"


def test_eval_low_precision_exits_4(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = lock_dev(tmp_path)
    capsys.readouterr()
    journal = tmp_path / "eval.jsonl"
    trials = [trial_row(f"eval-t{i:02d}", "target", 0.87 + 0.005 * i, 0.10) for i in range(8)]
    trials += [trial_row(f"eval-f{i:02d}", "nontarget", 0.90, 0.10) for i in range(4)]
    trials += [trial_row(f"eval-n{i:02d}", "nontarget", 0.07 + 0.005 * i, 0.02) for i in range(8)]

    with pytest.raises(SystemExit) as failure:
        calibrate_eval([write_journal(journal, trials)], out)

    assert failure.value.code == 4
    metrics = json.loads(capsys.readouterr().out)
    assert metrics["pass"] is False
    assert metrics["precision"] < 0.90
    assert metrics["wilson_lower"] < 0.98


@pytest.mark.parametrize(
    ("argv", "target", "expected"),
    [
        (
            ["calibrate", "lock", "--dev", "a.jsonl", "--dev", "b.jsonl", "--out", "c.json"],
            "calibrate_lock",
            {"dev_journals": ["a.jsonl", "b.jsonl"], "out": "c.json"},
        ),
        (
            ["calibrate", "eval", "--eval", "e.jsonl", "--artifact", "c.json"],
            "calibrate_eval",
            {"eval_journals": ["e.jsonl"], "artifact": "c.json"},
        ),
    ],
)
def test_calibrate_commands_dispatch_journals_and_outputs(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], target: str, expected: dict[str, Any]
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_command(*args: Any, **kwargs: Any) -> None:
        assert args == ()
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, target, fake_command)
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert {key: call[key] for key in expected} == expected


CALIBRATE_DIGEST_A = "c" * 64
CALIBRATE_DIGEST_B = "d" * 64


def _lock_stamped(tmp_path: Path, model_version: str) -> str:
    first = tmp_path / "dev-a.jsonl"
    second = tmp_path / "dev-b.jsonl"
    trials = dev_trials()
    out = str(tmp_path / "artifact.json")
    calibrate_lock(
        [write_journal(first, trials[:12]), write_journal(second, trials[12:])],
        out,
        model_version=model_version,
    )
    return out


def test_lock_model_version_flag_recorded_in_artifact(tmp_path: Path) -> None:
    stamped = f"model-a.onnx:{CALIBRATE_DIGEST_A}"
    out = _lock_stamped(tmp_path, stamped)
    payload = json.loads(Path(out).read_text())
    assert payload["model_version"] == stamped
    artifact = load_calibration_artifact(out)
    assert artifact is not None
    assert artifact.model_version == stamped


def test_lock_garbage_model_version_fails_without_artifact(tmp_path: Path) -> None:
    first = tmp_path / "dev-a.jsonl"
    out = str(tmp_path / "artifact.json")
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.CALIBRATE_MODEL_DIGEST_MISSING)
    ) as failure:
        calibrate_lock(
            [write_journal(first, dev_trials())],
            out,
            model_version="not-a-digest",
        )
    assert failure.value.code is ErrorCode.CALIBRATE_MODEL_DIGEST_MISSING
    assert failure.value.exit_code is ExitCode.INPUT_REJECTED
    assert not Path(out).exists()


def test_eval_model_mismatch_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = _lock_stamped(tmp_path, f"model-a.onnx:{CALIBRATE_DIGEST_A}")
    journal = tmp_path / "eval.jsonl"
    capsys.readouterr()
    with pytest.raises(SystemExit) as failure:
        calibrate_eval(
            [write_journal(journal, pass_trials())],
            out,
            model_version=f"model-b.onnx:{CALIBRATE_DIGEST_B}",
        )
    assert failure.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "model_mismatch"


def test_eval_matching_model_version_proceeds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stamped = f"model-a.onnx:{CALIBRATE_DIGEST_A}"
    out = _lock_stamped(tmp_path, stamped)
    journal = tmp_path / "eval.jsonl"
    capsys.readouterr()
    calibrate_eval(
        [write_journal(journal, pass_trials())],
        out,
        model_version=f"other-name.onnx:{CALIBRATE_DIGEST_A}",
    )
    assert json.loads(capsys.readouterr().out)["pass"] is True


def test_eval_flag_against_an_artifact_for_another_model_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = lock_dev(tmp_path)
    journal = tmp_path / "eval.jsonl"
    capsys.readouterr()
    with pytest.raises(SystemExit) as failure:
        calibrate_eval(
            [write_journal(journal, pass_trials())],
            out,
            model_version=f"model-a.onnx:{CALIBRATE_DIGEST_A}",
        )
    assert failure.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "model_unknown"


def test_eval_valley_ignores_overlap_trials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = lock_dev(tmp_path)
    artifact = load_calibration_artifact(out)
    assert artifact is not None
    mean = statistics.mean(artifact.snorm_cohort)
    std = statistics.pstdev(artifact.snorm_cohort)
    on_theta = mean + std * (artifact.theta_llr - artifact.platt_b) / artifact.platt_a
    trials = pass_trials() + [trial_row("eval-ox", "target", on_theta, 0.10, overlap=True)]
    journal = tmp_path / "eval.jsonl"
    capsys.readouterr()
    calibrate_eval([write_journal(journal, trials)], out)
    metrics = json.loads(capsys.readouterr().out)
    assert metrics["valley"] is True
    assert metrics["pass"] is True
