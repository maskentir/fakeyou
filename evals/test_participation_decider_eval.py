import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fakeyou.conversation.participation_decider import JevDecider

CORPUS = Path(__file__).resolve().parent / "fixtures" / "participation-corpus.json"


def summarize_policy(rows: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {"rule": 0, "jev": 0, "stale_pregen": 0}
    for row in rows:
        if row.get("event") != "policy_decided":
            continue
        source = row.get("decision_source", "rule")
        if source in summary:
            summary[source] += 1
        if row.get("reason") == "jev_stale_silent":
            summary["stale_pregen"] += 1
    return summary


def score_corpus(
    corpus: list[dict[str, Any]], transport: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
) -> dict[str, float]:
    async def _run() -> dict[str, float]:
        decider = JevDecider(
            api_key="test",
            model="jev-latest",
            base_url="https://test",
            timeout_seconds=1.0,
            transport=transport,
        )
        false_speak = 0
        false_silent = 0
        speak_den = 0
        silent_den = 0
        for row in corpus:
            decision = await decider.decide(row["text"])
            predicted = "speak" if decision.action == "speak" else "silent"
            if row["label"] == "speak":
                speak_den += 1
                if predicted != "speak":
                    false_silent += 1
            else:
                silent_den += 1
                if predicted == "speak":
                    false_speak += 1
        await decider.aclose()
        return {
            "false_speak_rate": (false_speak / silent_den) if silent_den else 0.0,
            "false_silent_rate": (false_silent / speak_den) if speak_den else 0.0,
        }

    return asyncio.run(_run())


def load_corpus() -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = json.loads(CORPUS.read_text(encoding="utf-8"))
    return data


def test_corpus_harness_measures_false_speak_and_silent() -> None:
    corpus = [
        {
            "id": "c1",
            "text": "Harun, you're up",
            "participant_id": "a",
            "label": "speak",
            "reason": "explicit cue",
        },
        {
            "id": "c2",
            "text": "kita lanjutin PR kemarin ya",
            "participant_id": "a",
            "label": "silent",
            "reason": "others conversing",
        },
    ]

    async def _speak(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "decision": {"choice": "speak", "confidence": 0.9, "probs": {}},
            "addressed_to_me": {"probability": 0.9},
            "others_conversing": {"probability": 0.0},
            "needs_clarify": {"probability": 0.0},
        }

    result = score_corpus(corpus, transport=_speak)
    assert result["false_speak_rate"] == 1.0
    assert result["false_silent_rate"] == 0.0


def test_report_summary_counts_rule_jev_and_stale() -> None:
    rows = [
        {"event": "policy_decided", "decision_source": "rule", "reason": "rule_addressed"},
        {"event": "policy_decided", "decision_source": "jev", "reason": "jev_speak"},
        {"event": "policy_decided", "decision_source": "jev", "reason": "jev_stale_silent"},
    ]
    assert summarize_policy(rows) == {"rule": 1, "jev": 2, "stale_pregen": 1}


def test_labeled_corpus_has_expected_shape() -> None:
    corpus = load_corpus()
    assert len(corpus) == 12
    for row in corpus:
        assert set(row) == {"id", "text", "participant_id", "label", "reason"}
        assert row["label"] in ("speak", "silent", "clarify")
