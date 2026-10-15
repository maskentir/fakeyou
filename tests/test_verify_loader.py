import json
from pathlib import Path

from fakeyou.identity.verify import load_calibration_artifact
from tests._verify_support import ARTIFACT, DIGEST_A


def test_loader_reads_versioned_artifact_from_file(tmp_path: Path) -> None:
    path = tmp_path / "calibration.json"
    path.write_text(
        json.dumps(
            {
                "theta_llr": 2.0,
                "margin_floor": 0.15,
                "snorm_cohort": [0.1, 0.2, 0.3, 0.4],
                "platt_a": 1.0,
                "platt_b": 0.0,
                "platt_version": "platt-v1",
                "report_id": "cal-001",
            }
        )
    )

    assert load_calibration_artifact(path) == ARTIFACT


def test_loader_missing_file_returns_null(tmp_path: Path) -> None:
    assert load_calibration_artifact(tmp_path / "absent.json") is None


def test_loader_malformed_file_returns_null(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert load_calibration_artifact(broken) is None
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"theta_llr": 2.0}))
    assert load_calibration_artifact(incomplete) is None
    typed = tmp_path / "typed.json"
    typed.write_text(
        json.dumps(
            {
                "theta_llr": "high",
                "margin_floor": 0.15,
                "snorm_cohort": [0.1, 0.2],
                "platt_a": 1.0,
                "platt_b": 0.0,
                "platt_version": "platt-v1",
                "report_id": "cal-001",
            }
        )
    )
    assert load_calibration_artifact(typed) is None
    empty = tmp_path / "empty.json"
    empty.write_text(
        json.dumps(
            {
                "theta_llr": 2.0,
                "margin_floor": 0.15,
                "snorm_cohort": [],
                "platt_a": 1.0,
                "platt_b": 0.0,
                "platt_version": "platt-v1",
                "report_id": "cal-001",
            }
        )
    )
    assert load_calibration_artifact(empty) is None


def test_loader_reads_model_version_from_file(tmp_path: Path) -> None:
    path = tmp_path / "stamped.json"
    stamped = f"model-a.onnx:{DIGEST_A}"
    path.write_text(
        json.dumps(
            {
                "theta_llr": 2.0,
                "margin_floor": 0.15,
                "snorm_cohort": [0.1, 0.2, 0.3, 0.4],
                "platt_a": 1.0,
                "platt_b": 0.0,
                "platt_version": "platt-v1",
                "report_id": "cal-001",
                "model_version": stamped,
            }
        )
    )
    loaded = load_calibration_artifact(path)
    assert loaded is not None
    assert loaded.model_version == stamped


def test_loader_rejects_invalid_model_version_type(tmp_path: Path) -> None:
    path = tmp_path / "badmodel.json"
    path.write_text(
        json.dumps(
            {
                "theta_llr": 2.0,
                "margin_floor": 0.15,
                "snorm_cohort": [0.1, 0.2, 0.3, 0.4],
                "platt_a": 1.0,
                "platt_b": 0.0,
                "platt_version": "platt-v1",
                "report_id": "cal-001",
                "model_version": 123,
            }
        )
    )
    assert load_calibration_artifact(path) is None
