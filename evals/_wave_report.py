from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TEMPLATE = Path(__file__).resolve().parent / "fixtures" / "wave-report-template.md"
NO_ARTIFACT = "-"
NO_GAPS = "-"


@dataclass(frozen=True, slots=True)
class GateRow:
    name: str
    command: str
    result: str
    baseline: str


@dataclass(frozen=True, kw_only=True, slots=True)
class WaveReportSpec:
    wave: int
    integration_commit: str
    gates: Sequence[GateRow]
    replay_summary: Mapping[str, Any] | None
    gaps: Sequence[str]
    live: Mapping[str, str]


def _replay_section(replay_summary: Mapping[str, Any] | None) -> str:
    if replay_summary is None:
        return NO_ARTIFACT
    lines = [
        f"- commit: `{replay_summary['source_commit']}`",
        f"- fixture: `{replay_summary['fixture_name']}`",
        f"- fixture_sha256: `{replay_summary['fixture_sha256']}`",
        f"- passed: {replay_summary['passed']}",
    ]
    lines.extend(
        f"- gate `{name}`: {replay_summary['gates'][name]}"
        for name in sorted(replay_summary["gates"])
    )
    return "\n".join(lines)


def render_wave_report(spec: WaveReportSpec) -> str:
    gate_rows = "\n".join(
        f"| {row.name} | `{row.command}` | {row.result} | {row.baseline} |" for row in spec.gates
    )
    gap_rows = "\n".join(f"- {gap}" for gap in spec.gaps) or NO_GAPS
    return TEMPLATE.read_text(encoding="utf-8").format(
        wave=spec.wave,
        integration_commit=spec.integration_commit,
        gate_rows=gate_rows,
        replay_section=_replay_section(spec.replay_summary),
        live_fungsi=spec.live["fungsi"],
        live_kualitas=spec.live["kualitas"],
        live_performa=spec.live["performa"],
        gap_rows=gap_rows,
    )
