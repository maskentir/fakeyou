from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.identity.calibration import (
    DCF_CFA,
    DCF_CMISS,
    DCF_PTAR,
    LLR_BIN_WIDTH,
    MARGIN_BIN_WIDTH,
    OVERLAP_MIN,
    PLATT_L2,
    PLATT_VERSION,
    RELIABILITY_BIN_COUNT,
    RELIABILITY_BOUND,
    WILSON_MIN,
    CalibrationEval,
    CalibrationLock,
    CalibrationRejection,
    RejectionReason,
    evaluate_calibration,
    lock_calibration,
    ship_gate,
)
from fakeyou.identity.verify import load_calibration_artifact
from tests._calibration_support import (
    dev_trials,
    lock_dev,
    pass_trials,
    trial_row,
    write_journal,
)
from tests._error_support import message_pattern


def test_pinned_constants_match_report() -> None:
    assert LLR_BIN_WIDTH == 0.5
    assert MARGIN_BIN_WIDTH == 0.05
    assert RELIABILITY_BIN_COUNT == 5
    assert RELIABILITY_BOUND == 0.15
    assert DCF_CMISS == 1.0
    assert DCF_CFA == 10.0
    assert DCF_PTAR == 0.5
    assert WILSON_MIN == 0.98
    assert OVERLAP_MIN == 0.95
    assert PLATT_L2 == 0.1
    assert PLATT_VERSION == "platt-v1"


def test_ship_gate_thresholds() -> None:
    good: dict[str, Any] = {
        "verified": 190,
        "wilson_lower": 0.9802,
        "overlap_rate": 1.0,
        "twin_alarms": 0,
        "valley": True,
    }

    assert ship_gate(**good) is True
    assert ship_gate(**{**good, "verified": 0}) is False
    assert ship_gate(**{**good, "wilson_lower": 0.9799}) is False
    assert ship_gate(**{**good, "wilson_lower": 0.98}) is True
    assert ship_gate(**{**good, "overlap_rate": 0.94}) is False
    assert ship_gate(**{**good, "overlap_rate": 0.95}) is True
    assert ship_gate(**{**good, "twin_alarms": 1}) is False
    assert ship_gate(**{**good, "valley": False}) is False


def test_lock_returns_the_summary_of_the_artifact_it_wrote(tmp_path: Path) -> None:
    trials = dev_trials()
    out = tmp_path / "artifact.json"

    lock = lock_calibration(
        [
            write_journal(tmp_path / "dev-a.jsonl", trials[:12]),
            write_journal(tmp_path / "dev-b.jsonl", trials[12:]),
        ],
        str(out),
    )

    artifact = load_calibration_artifact(out)
    assert artifact is not None
    assert lock == CalibrationLock(
        theta_llr=artifact.theta_llr,
        margin_floor=artifact.margin_floor,
        report_id=artifact.report_id,
    )


def test_lock_with_only_targets_is_rejected_before_writing(tmp_path: Path) -> None:
    journal = write_journal(
        tmp_path / "dev.jsonl", [trial_row(f"dev-t{i:02d}", "target", 0.9, 0.1) for i in range(4)]
    )
    out = tmp_path / "artifact.json"

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.CALIBRATE_DEV_TRIALS_INCOMPLETE)
    ) as failure:
        lock_calibration([journal], str(out))

    assert failure.value.code is ErrorCode.CALIBRATE_DEV_TRIALS_INCOMPLETE
    assert not out.exists()


def test_evaluate_reports_a_missing_artifact_as_rejection(tmp_path: Path) -> None:
    journal = write_journal(tmp_path / "eval.jsonl", pass_trials())

    outcome = evaluate_calibration([journal], str(tmp_path / "absent.json"))

    assert outcome == CalibrationRejection(RejectionReason.INVALID_ARTIFACT)


def test_evaluate_names_speakers_heard_in_both_dev_and_eval(tmp_path: Path) -> None:
    artifact = lock_dev(tmp_path)
    journal = write_journal(
        tmp_path / "eval.jsonl",
        [trial_row("dev-t00", "target", 0.90, 0.10), trial_row("eval-t01", "target", 0.90, 0.10)],
    )

    outcome = evaluate_calibration([journal], artifact)

    assert outcome == CalibrationRejection(
        RejectionReason.SPEAKER_OVERLAP, overlap_speakers=("dev-t00",)
    )


def test_evaluate_rejects_a_model_the_artifact_was_not_fitted_for(tmp_path: Path) -> None:
    artifact = str(tmp_path / "artifact.json")
    lock_calibration(
        [write_journal(tmp_path / "dev.jsonl", dev_trials())],
        artifact,
        model_version="sha256:" + "c" * 64,
    )
    journal = write_journal(tmp_path / "eval.jsonl", pass_trials())

    outcome = evaluate_calibration([journal], artifact, model_version="sha256:" + "d" * 64)

    assert outcome == CalibrationRejection(RejectionReason.MODEL_MISMATCH)


def test_evaluate_returns_metrics_for_a_passing_eval(tmp_path: Path) -> None:
    artifact = lock_dev(tmp_path)
    trials = pass_trials()
    journal = write_journal(tmp_path / "eval.jsonl", trials)

    outcome = evaluate_calibration([journal], artifact)

    assert isinstance(outcome, CalibrationEval)
    assert outcome.passed is True
    assert outcome.n_eval == len(trials)
    assert outcome.n_verified == 190
    assert outcome.precision == 1.0
    assert outcome.twin_false_verify == 0
    assert outcome.valley is True
    assert outcome.report_id.startswith("cal-")


def test_report_id_pins_the_canonical_trial_digest(tmp_path: Path) -> None:
    artifact = lock_dev(tmp_path)

    loaded = load_calibration_artifact(artifact)

    assert loaded is not None
    assert loaded.report_id == "cal-52c59d1bc38e"
