from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.output import emit_json
from fakeyou.identity.calibration import (
    CalibrationEval,
    CalibrationRejection,
    RejectionReason,
    evaluate_calibration,
    lock_calibration,
)

_REJECTION_EXIT_CODES: dict[RejectionReason, ExitCode] = {
    RejectionReason.INVALID_ARTIFACT: ExitCode.EVAL_GATE_FAILED,
    RejectionReason.MODEL_UNKNOWN: ExitCode.INPUT_REJECTED,
    RejectionReason.MODEL_MISMATCH: ExitCode.INPUT_REJECTED,
    RejectionReason.MISSING_DEV_SPEAKERS: ExitCode.INPUT_REJECTED,
    RejectionReason.SPEAKER_OVERLAP: ExitCode.INPUT_REJECTED,
    RejectionReason.DEGENERATE_COHORT: ExitCode.EVAL_GATE_FAILED,
}


def calibrate_lock(
    dev_journals: list[str],
    out: str,
    *,
    model_version: str | None = None,
) -> None:
    lock = lock_calibration(dev_journals, out, model_version=model_version)
    emit_json(
        {
            "artifact": out,
            "theta_llr": lock.theta_llr,
            "margin_floor": lock.margin_floor,
            "report_id": lock.report_id,
        }
    )


def calibrate_eval(
    eval_journals: list[str],
    artifact: str,
    *,
    model_version: str | None = None,
) -> None:
    outcome = evaluate_calibration(eval_journals, artifact, model_version=model_version)
    if isinstance(outcome, CalibrationRejection):
        payload: dict[str, object] = {"pass": False, "error": outcome.reason.value}
        if outcome.reason is RejectionReason.SPEAKER_OVERLAP:
            payload["overlap_speakers"] = list(outcome.overlap_speakers)
        emit_json(payload)
        raise SystemExit(_REJECTION_EXIT_CODES[outcome.reason])
    emit_json(_metrics_payload(outcome))
    if not outcome.passed:
        raise SystemExit(ExitCode.EVAL_GATE_FAILED)


def _metrics_payload(metrics: CalibrationEval) -> dict[str, object]:
    return {
        "pass": metrics.passed,
        "n_eval": metrics.n_eval,
        "n_verified": metrics.n_verified,
        "precision": metrics.precision,
        "wilson_lower": metrics.wilson_lower,
        "overlap_unverified_rate": metrics.overlap_unverified_rate,
        "twin_false_verify": metrics.twin_false_verify,
        "valley": metrics.valley,
        "theta_llr": metrics.theta_llr,
        "margin_floor": metrics.margin_floor,
        "report_id": metrics.report_id,
    }
