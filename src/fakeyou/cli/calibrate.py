from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.output import emit_json
from fakeyou.config.settings import Settings
from fakeyou.identity.verify import load_calibration_artifact, model_digest

LLR_BIN_WIDTH = 0.5
MARGIN_BIN_WIDTH = 0.05
RELIABILITY_BIN_COUNT = 5
RELIABILITY_BOUND = 0.15
DCF_CMISS = 1.0
DCF_CFA = 10.0
DCF_PTAR = 0.5
WILSON_MIN = 0.98
OVERLAP_MIN = 0.95
WILSON_Z = 1.96
PLATT_L2 = 0.1
PLATT_MAX_ITER = 100
PLATT_TOL = 1e-12
PLATT_VERSION = "platt-v1"


@dataclass(frozen=True, slots=True)
class Trial:
    speaker: str
    target: bool
    s1: float
    s2: float
    overlap: bool
    claim_agree: bool
    twin: bool


def _fail_eval(payload: dict[str, Any], code: ExitCode) -> NoReturn:
    emit_json(payload)
    raise SystemExit(code)


def _trial_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _trial_flag(value: Any, default: bool) -> bool | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return None


def _trial_of(payload: Any, origin: str) -> Trial:
    if not isinstance(payload, dict):
        raise FakeYouError(ErrorCode.CALIBRATE_TRIAL_NOT_OBJECT, origin=origin)
    speaker = payload.get("speaker_id")
    if not isinstance(speaker, str) or not speaker:
        raise FakeYouError(ErrorCode.CALIBRATE_TRIAL_SPEAKER_MISSING, origin=origin)
    label = payload.get("label")
    if label == "target":
        target = True
    elif label == "nontarget":
        target = False
    else:
        raise FakeYouError(ErrorCode.CALIBRATE_TRIAL_LABEL_INVALID, origin=origin)
    s1 = _trial_number(payload.get("s1"))
    s2 = _trial_number(payload.get("s2"))
    if s1 is None or s2 is None:
        raise FakeYouError(ErrorCode.CALIBRATE_TRIAL_SCORES_INVALID, origin=origin)
    overlap = _trial_flag(payload.get("overlap"), False)
    claim_agree = _trial_flag(payload.get("claim_agree"), False)
    twin = _trial_flag(payload.get("twin"), False)
    if overlap is None or claim_agree is None or twin is None:
        raise FakeYouError(ErrorCode.CALIBRATE_TRIAL_FLAGS_INVALID, origin=origin)
    return Trial(
        speaker=speaker,
        target=target,
        s1=s1,
        s2=s2,
        overlap=overlap,
        claim_agree=claim_agree,
        twin=twin,
    )


def _load_trials(paths: list[str]) -> list[Trial]:
    trials: list[Trial] = []
    for path in paths:
        try:
            text = Path(path).read_text()
        except OSError:
            raise FakeYouError(ErrorCode.CALIBRATE_JOURNAL_UNREADABLE, path=path) from None
        for lineno, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            origin = f"{path}:{lineno}"
            try:
                payload = json.loads(line)
            except ValueError:
                raise FakeYouError(
                    ErrorCode.CALIBRATE_JOURNAL_LINE_INVALID, origin=origin
                ) from None
            trials.append(_trial_of(payload, origin))
    return trials


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    peaked = math.exp(value)
    return peaked / (1.0 + peaked)


def _snorm_stats(cohort: list[float]) -> tuple[float, float] | None:
    if not cohort:
        return None
    mean = statistics.mean(cohort)
    std = statistics.pstdev(cohort)
    if std <= 0.0:
        return None
    return mean, std


def _fit_platt(links: list[float], labels: list[float]) -> tuple[float, float]:
    slope = 0.0
    bias = 0.0
    for _ in range(PLATT_MAX_ITER):
        grad_slope = 0.0
        grad_bias = 0.0
        hess_ss = 0.0
        hess_sb = 0.0
        hess_bb = 0.0
        for link, label in zip(links, labels, strict=True):
            prob = _sigmoid(slope * link + bias)
            weight = prob * (1.0 - prob)
            resid = label - prob
            grad_slope += resid * link
            grad_bias += resid
            hess_ss += weight * link * link
            hess_sb += weight * link
            hess_bb += weight
        grad_slope -= PLATT_L2 * slope
        grad_bias -= PLATT_L2 * bias
        hess_ss += PLATT_L2
        hess_bb += PLATT_L2
        det = hess_ss * hess_bb - hess_sb * hess_sb
        if det <= 0.0:
            break
        step_slope = (grad_slope * hess_bb - grad_bias * hess_sb) / det
        step_bias = (hess_ss * grad_bias - hess_sb * grad_slope) / det
        slope += step_slope
        bias += step_bias
        if abs(step_slope) + abs(step_bias) < PLATT_TOL:
            break
    return slope, bias


def _reliability_holds(llrs: list[float], labels: list[float]) -> bool:
    sums = [0.0] * RELIABILITY_BIN_COUNT
    hits = [0] * RELIABILITY_BIN_COUNT
    counts = [0] * RELIABILITY_BIN_COUNT
    for llr, label in zip(llrs, labels, strict=True):
        prob = _sigmoid(llr)
        index = min(int(prob * RELIABILITY_BIN_COUNT), RELIABILITY_BIN_COUNT - 1)
        sums[index] += prob
        hits[index] += int(label)
        counts[index] += 1
    for index in range(RELIABILITY_BIN_COUNT):
        if not counts[index]:
            continue
        predicted = sums[index] / counts[index]
        observed = hits[index] / counts[index]
        if abs(predicted - observed) > RELIABILITY_BOUND:
            return False
    return True


def _dcf(theta: float, llrs: list[float], labels: list[float]) -> float:
    miss = 0
    miss_total = 0
    alarm = 0
    alarm_total = 0
    for llr, label in zip(llrs, labels, strict=True):
        if label > 0.5:
            miss_total += 1
            if llr < theta:
                miss += 1
        else:
            alarm_total += 1
            if llr >= theta:
                alarm += 1
    pmiss = miss / miss_total if miss_total else 1.0
    pfa = alarm / alarm_total if alarm_total else 1.0
    return DCF_CMISS * pmiss * DCF_PTAR + DCF_CFA * pfa * (1.0 - DCF_PTAR)


def _scan_theta(llrs: list[float], labels: list[float]) -> float | None:
    ordered = sorted(set(llrs))
    if len(ordered) < 2:
        return None
    scored = [(ordered[i] + ordered[i + 1]) / 2.0 for i in range(len(ordered) - 1)]
    costs = [_dcf(theta, llrs, labels) for theta in scored]
    floor = min(costs)
    tied = sorted(theta for theta, cost in zip(scored, costs, strict=True) if cost == floor)
    return tied[len(tied) // 2]


def _valley_holds(values: list[float], point: float, width: float) -> bool:
    filled = {math.floor(value / width) for value in values}
    slot = math.floor(point / width)
    return slot not in filled and slot - 1 not in filled and slot + 1 not in filled


def _wilson_lower(hits: int, total: int) -> float:
    if total <= 0:
        return 0.0
    rate = hits / total
    denom = 1.0 + WILSON_Z * WILSON_Z / total
    center = rate + WILSON_Z * WILSON_Z / (2.0 * total)
    inner = (rate * (1.0 - rate) + WILSON_Z * WILSON_Z / (4.0 * total)) / total
    spread = WILSON_Z * math.sqrt(inner)
    return max(0.0, (center - spread) / denom)


def _report_id(trials: list[Trial]) -> str:
    rows = sorted(
        (
            {
                "speaker_id": trial.speaker,
                "label": "target" if trial.target else "nontarget",
                "s1": trial.s1,
                "s2": trial.s2,
                "overlap": trial.overlap,
                "claim_agree": trial.claim_agree,
                "twin": trial.twin,
            }
            for trial in trials
        ),
        key=lambda row: (row["speaker_id"], row["label"], row["s1"], row["s2"]),
    )
    digest = hashlib.sha1(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:12]
    return f"cal-{digest}"


def calibrate_lock(
    dev_journals: list[str],
    out: str,
    *,
    settings: Settings,
    model_version: str | None = None,
) -> None:
    if model_version is not None and model_digest(model_version) is None:
        raise FakeYouError(ErrorCode.CALIBRATE_MODEL_DIGEST_MISSING, model_version=model_version)
    trials = _load_trials(dev_journals)
    targets = [trial for trial in trials if trial.target]
    nontargets = [trial for trial in trials if not trial.target]
    if not targets or not nontargets:
        raise FakeYouError(ErrorCode.CALIBRATE_DEV_TRIALS_INCOMPLETE)
    stats = _snorm_stats(sorted(trial.s1 for trial in nontargets))
    if stats is None:
        raise FakeYouError(ErrorCode.CALIBRATE_COHORT_NO_SPREAD)
    mean, std = stats
    links = [(trial.s1 - mean) / std for trial in trials]
    labels = [1.0 if trial.target else 0.0 for trial in trials]
    slope, bias = _fit_platt(links, labels)
    llrs = [slope * link + bias for link in links]
    if not _reliability_holds(llrs, labels):
        raise FakeYouError(ErrorCode.CALIBRATE_RELIABILITY_EXCEEDS_BOUND)
    theta = _scan_theta(llrs, labels)
    if theta is None:
        raise FakeYouError(ErrorCode.CALIBRATE_NO_OPERATING_POINT)
    if not _valley_holds(llrs, theta, LLR_BIN_WIDTH):
        raise FakeYouError(ErrorCode.CALIBRATE_THETA_OUTSIDE_VALLEY)
    target_margins = [trial.s1 - trial.s2 for trial in targets]
    nontarget_margins = [trial.s1 - trial.s2 for trial in nontargets]
    worst_nontarget = max(nontarget_margins)
    best_target = min(target_margins)
    if worst_nontarget >= best_target:
        raise FakeYouError(ErrorCode.CALIBRATE_MARGINS_NO_VALLEY)
    floor = (worst_nontarget + best_target) / 2.0
    margins = target_margins + nontarget_margins
    if not _valley_holds(margins, floor, MARGIN_BIN_WIDTH):
        raise FakeYouError(ErrorCode.CALIBRATE_MARGIN_FLOOR_OUTSIDE_VALLEY)
    report = _report_id(trials)
    payload = {
        "theta_llr": theta,
        "margin_floor": floor,
        "snorm_cohort": sorted(trial.s1 for trial in nontargets),
        "platt_a": slope,
        "platt_b": bias,
        "platt_version": PLATT_VERSION,
        "report_id": report,
        "dev_speaker_ids": sorted({trial.speaker for trial in trials}),
    }
    if model_version is not None:
        payload["model_version"] = model_version
    try:
        Path(out).write_text(json.dumps(payload))
    except OSError:
        raise FakeYouError(ErrorCode.CALIBRATE_ARTIFACT_NOT_WRITTEN, path=out) from None
    summary = {"artifact": out, "theta_llr": theta, "margin_floor": floor, "report_id": report}
    emit_json(summary)


def _dev_speakers(artifact_path: str) -> list[str] | None:
    try:
        raw = json.loads(Path(artifact_path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    speakers = raw.get("dev_speaker_ids")
    if not isinstance(speakers, list) or not speakers:
        return None
    if any(not isinstance(entry, str) or not entry for entry in speakers):
        return None
    return list(speakers)


def _ship_gate(
    *,
    verified: int,
    wilson_lower: float,
    overlap_rate: float,
    twin_alarms: int,
    valley: bool,
) -> bool:
    return (
        verified > 0
        and wilson_lower >= WILSON_MIN
        and overlap_rate >= OVERLAP_MIN
        and twin_alarms == 0
        and valley
    )


def calibrate_eval(
    eval_journals: list[str],
    artifact: str,
    *,
    settings: Settings,
    model_version: str | None = None,
) -> None:
    loaded = load_calibration_artifact(artifact)
    if loaded is None:
        _fail_eval({"pass": False, "error": "invalid_artifact"}, ExitCode.EVAL_GATE_FAILED)
    if model_version is not None:
        if loaded.model_version is None:
            _fail_eval({"pass": False, "error": "model_unknown"}, ExitCode.INPUT_REJECTED)
        claimed = model_digest(model_version)
        if claimed is None or claimed != model_digest(loaded.model_version):
            _fail_eval({"pass": False, "error": "model_mismatch"}, ExitCode.INPUT_REJECTED)
    trials = _load_trials(eval_journals)
    dev = _dev_speakers(artifact)
    if dev is None:
        _fail_eval({"pass": False, "error": "missing_dev_speakers"}, ExitCode.INPUT_REJECTED)
    heard = sorted({trial.speaker for trial in trials} & set(dev))
    if heard:
        _fail_eval(
            {"pass": False, "error": "speaker_overlap", "overlap_speakers": heard},
            ExitCode.INPUT_REJECTED,
        )
    stats = _snorm_stats(list(loaded.snorm_cohort))
    if stats is None:
        _fail_eval({"pass": False, "error": "degenerate_cohort"}, ExitCode.EVAL_GATE_FAILED)
    mean, std = stats
    llrs: list[float] = []
    flags: list[bool] = []
    for trial in trials:
        llr = loaded.platt_a * (trial.s1 - mean) / std + loaded.platt_b
        if not trial.overlap:
            llrs.append(llr)
        margin = trial.s1 - trial.s2
        flags.append(
            llr >= loaded.theta_llr
            and margin >= loaded.margin_floor
            and not trial.overlap
            and trial.claim_agree
        )
    hits = sum(1 for trial, flag in zip(trials, flags, strict=True) if flag and trial.target)
    alarms = sum(1 for trial, flag in zip(trials, flags, strict=True) if flag and not trial.target)
    verified = hits + alarms
    precision = hits / verified if verified else None
    lower = _wilson_lower(hits, verified)
    overlapped = [trial for trial in trials if trial.overlap]
    held = sum(1 for trial, flag in zip(trials, flags, strict=True) if trial.overlap and not flag)
    overlap_rate = held / len(overlapped) if overlapped else 0.0
    twin_alarms = sum(1 for trial, flag in zip(trials, flags, strict=True) if flag and trial.twin)
    valley = _valley_holds(llrs, loaded.theta_llr, LLR_BIN_WIDTH)
    passed = _ship_gate(
        verified=verified,
        wilson_lower=lower,
        overlap_rate=overlap_rate,
        twin_alarms=twin_alarms,
        valley=valley,
    )
    metrics: dict[str, Any] = {
        "pass": passed,
        "n_eval": len(trials),
        "n_verified": verified,
        "precision": round(precision, 4) if precision is not None else None,
        "wilson_lower": round(lower, 4),
        "overlap_unverified_rate": round(overlap_rate, 4),
        "twin_false_verify": twin_alarms,
        "valley": valley,
        "theta_llr": loaded.theta_llr,
        "margin_floor": loaded.margin_floor,
        "report_id": loaded.report_id,
    }
    emit_json(metrics)
    if not passed:
        raise SystemExit(ExitCode.EVAL_GATE_FAILED)
