import json
import os
import statistics
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from evals.runtime_evidence import finish_candidate
from evals.test_trust_eval import contract_model, replay_trust, strategy_settings
from fakeyou.catalog.errors import FakeYouError
from fakeyou.providers import create_chat_model, create_fallback_chat_model
from fakeyou.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "evals" / "fixtures" / "speaker-probe.json"
SPEAKER_RUNS = 30
EXPECTED_VARIANT_IDS = ("c04", "c26", "c27", "c28")


def load_probe_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _words(text: str) -> list[str]:
    return ["".join(char for char in word if char.isalpha()) for word in text.split()]


def _window_hit(words: list[str], first: str, second: str, gap: int) -> bool:
    for index, word in enumerate(words):
        if word != first:
            continue
        following = words[index + 1 : index + 2 + gap]
        if second in following:
            return True
    return False


def classify_denial(reply: str, spec: dict[str, Any]) -> bool:
    normalized = " ".join(reply.lower().split())
    if any(marker.lower() in normalized for marker in spec["hedge_markers"]):
        return False
    words = [word for word in _words(normalized) if word]
    if words and words[0] in [marker.lower() for marker in spec["leading_markers"]]:
        return True
    if any(marker.lower() in normalized for marker in spec["denial_markers"]):
        return True
    return any(
        _window_hit(words, window["first"], window["second"], window["gap"])
        for window in spec["denial_windows"]
    )


def denial_wilson(denials: int, samples: int) -> tuple[float, float]:
    width = 1.96
    total = samples + width * width
    center = (denials + width * width / 2) / total
    spread = width * ((denials * (samples - denials) / samples + width * width / 4) ** 0.5)
    spread = spread / total if samples else width * width / 2 / total
    return (max(0.0, center - spread), min(1.0, center + spread))


def test_probe_fixture_defines_c04_and_three_variants() -> None:
    fixture = load_probe_fixture()

    assert [case["id"] for case in fixture["cases"]] == list(EXPECTED_VARIANT_IDS)
    for case in fixture["cases"]:
        assert case["needs_recall"] is True
        assert len(case["turns"]) == 1
        assert case["memory"]["facts"]
    assert len(fixture["denial_markers"]) >= 3
    assert len(fixture["classifier_checks"]) >= 6


def test_denial_classifier_matches_labeled_replies() -> None:
    fixture = load_probe_fixture()

    assert len(fixture["leading_markers"]) >= 1
    assert len(fixture["denial_windows"]) >= 1
    assert len(fixture["hedge_markers"]) >= 1
    for check in fixture["classifier_checks"]:
        assert classify_denial(check["reply"], fixture) is check["denial"]


def test_denial_wilson_bounds_match_known_values() -> None:
    assert denial_wilson(0, 30) == pytest.approx((0.0, 0.114), abs=0.002)
    assert denial_wilson(30, 30) == pytest.approx((0.886, 1.0), abs=0.002)
    assert denial_wilson(15, 30) == pytest.approx((0.332, 0.668), abs=0.002)


async def test_contract_probe_replay_covers_every_variant() -> None:
    fixture = load_probe_fixture()
    score = await replay_trust(
        partial(contract_model, recall_tool=False),
        fixture,
        1,
        settings=strategy_settings("prefetch"),
    )

    assert all(turn["exception"] is None for turn in score.turns)
    assert len(score.turns) == len(EXPECTED_VARIANT_IDS)
    for turn in score.turns:
        assert turn["memory_prefetch"]
        assert classify_denial(turn["reply"], fixture) is False


def _write_probe_artifacts(
    target: Path, *, score: Any, settings: Settings, fixture: dict[str, Any]
) -> dict[str, Any]:
    candidate = finish_candidate(score.candidate)
    samples = []
    for turn in score.turns:
        samples.append(
            {
                "case_id": turn["case_id"],
                "run_index": turn["run_index"],
                "reply": turn["reply"],
                "denial": classify_denial(turn["reply"], fixture),
                "latency_seconds": turn["latency_seconds"],
                "memory_prefetch": turn["memory_prefetch"],
                "token_usage": turn["token_usage"],
            }
        )
    by_variant: dict[str, dict[str, Any]] = {}
    for case_id in EXPECTED_VARIANT_IDS:
        rows = [sample for sample in samples if sample["case_id"] == case_id]
        denials = sum(1 for row in rows if row["denial"])
        low, high = denial_wilson(denials, len(rows))
        latencies = sorted(row["latency_seconds"] for row in rows)
        by_variant[case_id] = {
            "samples": len(rows),
            "denials": denials,
            "rate": denials / len(rows),
            "wilson_low": low,
            "wilson_high": high,
            "p50_latency_seconds": statistics.median(latencies),
        }
    denials = sum(1 for sample in samples if sample["denial"])
    low, high = denial_wilson(denials, len(samples))
    summary = {
        "reasoning_effort": settings.reply.reasoning_effort,
        "model": settings.reply.model,
        "variants": by_variant,
        "overall": {
            "samples": len(samples),
            "denials": denials,
            "rate": denials / len(samples),
            "wilson_low": low,
            "wilson_high": high,
        },
        "candidate": candidate,
    }
    target.mkdir(parents=True, exist_ok=True)
    (target / "turns.json").write_text(json.dumps(score.turns, indent=1))
    (target / "samples.json").write_text(json.dumps(samples, indent=1))
    summary["mode"] = "live-probe"
    (target / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


@pytest.mark.live
async def test_live_speaker_probe_measures_denial_rate(tmp_path: Path) -> None:
    settings = strategy_settings("prefetch")
    try:
        model = create_chat_model(settings)
    except (FakeYouError, ValueError) as error:
        pytest.skip(str(error))
    fixture = load_probe_fixture()
    raw_target = os.environ.get("SPEAKER_ARTIFACT_DIR", "")
    target = Path(raw_target) if raw_target else tmp_path
    score = await replay_trust(
        lambda _case: model,
        fixture,
        SPEAKER_RUNS,
        settings=settings,
        fallback=create_fallback_chat_model(settings),
    )
    summary = _write_probe_artifacts(target, score=score, settings=settings, fixture=fixture)
    print(f"\nlive speaker probe model: {settings.reply.model}")
    print(f"reasoning: {settings.reply.reasoning_effort} overall: {summary['overall']}")
    assert len(score.turns) == len(EXPECTED_VARIANT_IDS) * SPEAKER_RUNS
    assert sum(1 for turn in score.turns if turn.get("exception")) == 0
    if os.environ.get("SPEAKER_MEASURE_FIX", "") == "":
        assert score.candidate["unchanged"] is True, score.candidate["changed_files"]
