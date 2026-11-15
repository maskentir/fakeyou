from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fakeyou.foundation.claims import Claim, VerifiedClaim, support_claim
from fakeyou.foundation.publication import EvidenceBinding
from fakeyou.foundation.rendering import render
from fakeyou.foundation.statement import Tense
from fakeyou.perception.inspector import VisualInspection

ROOT = Path(__file__).parent.parent
LABELS = Path(__file__).parent / "fixtures" / "support-matcher" / "labels.json"
RESULTS = Path(__file__).parent / "fixtures" / "support-matcher" / "results.json"
SYNTHETIC = ROOT / "tests" / "fixtures" / "claim-support-cases.json"
Basis = Literal["answer", "answer_and_observed_facts"]
BASES: tuple[Basis, ...] = ("answer", "answer_and_observed_facts")
LANGUAGES = ("en", "id")
MISSING_EVIDENCE = frozenset({"validation_support_unknown", "claim_evidence_missing"})
AMBIGUOUS_RELATION = frozenset({"claim_relation_ambiguous"})
UNSUPPORTED_SHAPE = "claim_render_unsupported"


@dataclass(frozen=True, slots=True)
class Outcome:
    language: str
    gold: bool
    reason: str
    renderable: bool

    @property
    def spoken(self) -> bool:
        return self.reason == "ok" and self.renderable


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inspection(source: str, line: int) -> VisualInspection:
    row = json.loads((ROOT / source).read_text().splitlines()[line])
    if "text" in row:
        return VisualInspection.model_validate_json(row["text"])
    body = json.loads("".join(chunk[1] for chunk in row["chunks"]))
    return VisualInspection.model_validate_json(
        body["candidates"][0]["content"]["parts"][0]["text"]
    )


def _evidence_text(inspection: VisualInspection, basis: Basis) -> str | None:
    if inspection.answer is None:
        return None
    if basis == "answer":
        return inspection.answer
    return "\n".join((inspection.answer, *inspection.observed_facts))


def _status(inspection: VisualInspection) -> str:
    return "produced" if inspection.verdict == "supported" else "insufficient"


def _outcome(claim: Claim, text: str | None, status: str, language: str, *, gold: bool) -> Outcome:
    support = support_claim(claim, text, status=status, language=language)
    verified = VerifiedClaim(claim, None, support, text)
    tense: Tense = "current" if claim.source == "screen" else "historical"
    renderable = support.supported and (
        render(_with_evidence(verified), tense, language) is not None
    )
    return Outcome(language, gold, support.reason, renderable)


PLACEHOLDER_LOOK = EvidenceBinding("screen", "evaluation", None, 0.0, None, None, "inference")


def _with_evidence(verified: VerifiedClaim) -> VerifiedClaim:
    return VerifiedClaim(verified.claim, PLACEHOLDER_LOOK, verified.support, verified.answer)


def cassette_outcomes(basis: Basis) -> list[Outcome]:
    labels = json.loads(LABELS.read_text())
    outcomes: list[Outcome] = []
    for look in labels["looks"]:
        inspection = _inspection(look["source"], look["line"])
        text = _evidence_text(inspection, basis)
        for item in look["claims"]:
            claim = Claim.model_validate(item["claim"])
            outcomes.append(
                _outcome(
                    claim,
                    text,
                    _status(inspection),
                    look["language"],
                    gold=bool(item["true_of_frame"]),
                )
            )
    return outcomes


def synthetic_outcomes() -> list[Outcome]:
    cases = json.loads(SYNTHETIC.read_text())["cases"]
    return [
        _outcome(
            Claim.model_validate(case["claim"]),
            case["answer"],
            case["status"],
            case["language"],
            gold=case["expected"] == "ok",
        )
        for case in cases
    ]


def _rate(part: int, whole: int) -> float | None:
    return None if whole == 0 else round(part / whole, 4)


def summarize(outcomes: list[Outcome]) -> dict[str, Any]:
    true = [item for item in outcomes if item.gold]
    false = [item for item in outcomes if not item.gold]
    silent = [item for item in outcomes if not item.spoken]
    reasons = Counter(UNSUPPORTED_SHAPE if item.reason == "ok" else item.reason for item in silent)
    categories = {
        "missing_evidence": sum(reasons[name] for name in MISSING_EVIDENCE),
        "ambiguous_relation": sum(reasons[name] for name in AMBIGUOUS_RELATION),
        "unsupported_shape": reasons[UNSUPPORTED_SHAPE],
    }
    categories["other_unsupported"] = len(silent) - sum(categories.values())
    return {
        "claims": len(outcomes),
        "true_claims": len(true),
        "false_claims": len(false),
        "false_support": sum(item.reason == "ok" for item in false),
        "false_support_rate": _rate(sum(item.reason == "ok" for item in false), len(false)),
        "false_rejection": sum(not item.spoken for item in true),
        "false_rejection_rate": _rate(sum(not item.spoken for item in true), len(true)),
        "coverage": _rate(sum(item.spoken for item in true), len(true)),
        "abstention_rate": _rate(len(silent), len(outcomes)),
        "abstentions_by_category": categories,
        "abstentions_by_reason": dict(sorted(reasons.items())),
    }


def by_language(outcomes: list[Outcome]) -> dict[str, Any]:
    return {
        language: summarize([item for item in outcomes if item.language == language])
        for language in LANGUAGES
    }


def evaluate() -> dict[str, Any]:
    labels = json.loads(LABELS.read_text())
    return {
        "version": 1,
        "inputs": labels["inputs"],
        "provider_requests": 0,
        "cassette_looks": len(labels["looks"]),
        "cassette": {basis: by_language(cassette_outcomes(basis)) for basis in BASES},
        "synthetic": by_language(synthetic_outcomes()),
    }
