from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict, Unpack

from fakeyou.identity.calibration import lock_calibration


class TrialFlags(TypedDict, total=False):
    overlap: bool
    twin: bool


def trial_row(
    speaker: str, label: str, s1: float, s2: float, **flags: Unpack[TrialFlags]
) -> dict[str, Any]:
    return {
        "speaker_id": speaker,
        "label": label,
        "s1": s1,
        "s2": s2,
        "overlap": flags.get("overlap", False),
        "claim_agree": True,
        "twin": flags.get("twin", False),
    }


def write_journal(path: Path, trials: list[dict[str, Any]]) -> str:
    path.write_text("\n".join(json.dumps(row) for row in trials) + "\n")
    return str(path)


def dev_trials() -> list[dict[str, Any]]:
    trials = [trial_row(f"dev-t{i:02d}", "target", 0.86 + 0.01 * i, 0.10) for i in range(12)]
    trials += [trial_row(f"dev-n{i:02d}", "nontarget", 0.06 + 0.01 * i, 0.02) for i in range(12)]
    return trials


def pass_trials() -> list[dict[str, Any]]:
    trials = [trial_row(f"eval-t{i:03d}", "target", 0.86 + 0.0005 * i, 0.10) for i in range(190)]
    trials += [trial_row(f"eval-n{i:02d}", "nontarget", 0.06 + 0.005 * i, 0.02) for i in range(20)]
    trials += [trial_row(f"eval-o{i:02d}", "target", 0.95, 0.10, overlap=True) for i in range(10)]
    trials += [trial_row(f"eval-w{i:02d}", "nontarget", 0.25, 0.12, twin=True) for i in range(4)]
    return trials


def lock_dev(tmp_path: Path) -> str:
    first = tmp_path / "dev-a.jsonl"
    second = tmp_path / "dev-b.jsonl"
    trials = dev_trials()
    out = str(tmp_path / "artifact.json")
    lock_calibration(
        [write_journal(first, trials[:12]), write_journal(second, trials[12:])],
        out,
    )
    return out
