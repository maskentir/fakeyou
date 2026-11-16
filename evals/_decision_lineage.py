import argparse
import functools
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from fakeyou.debrief.debrief_schema import (
    DebriefReport,
    DebriefValidationError,
    ItemKind,
    ItemStatus,
)
from fakeyou.debrief.register import DecisionRecord, MeetingScope, records_from_report

FIXTURE = Path(__file__).parent / "fixtures" / "decision-lineage-pairs.json"

Label = Literal[
    "related_discussion",
    "potential_contradiction",
    "potential_supersession",
    "no_established_relationship",
    "abstain",
]
LABELS: tuple[Label, ...] = get_args(Label)
RELATIONSHIPS: tuple[Label, ...] = (
    "related_discussion",
    "potential_contradiction",
    "potential_supersession",
)
Language = Literal["en", "id", "mixed"]
TemporalOrder = Literal["strictly_earlier", "equal"]
Defect = Literal["altered_quote", "missing_source"]
Category = Literal[
    "related",
    "contradiction",
    "supersession",
    "hard_negative",
    "lexical_overlap",
    "paraphrase",
    "cross_language",
    "code_mixed",
    "owner_approval_claim",
    "ambiguous",
    "unattributed",
    "equal_timestamps",
    "altered_evidence",
    "missing_evidence",
    "prompt_injection",
]
_Text = Annotated[str, StringConstraints(min_length=1)]
_STRICT = ConfigDict(extra="forbid", frozen=True)


class FixtureSource(BaseModel):
    model_config = _STRICT

    id: _Text
    participant_id: _Text
    text: _Text


class FixtureEvidence(BaseModel):
    model_config = _STRICT

    entry_id: _Text
    quote: _Text


class FixtureItem(BaseModel):
    model_config = _STRICT

    key: _Text
    kind: ItemKind
    status: ItemStatus
    text: _Text
    assignee_claim: str | None
    due_text: str | None
    evidence: tuple[FixtureEvidence, ...]


class FixtureMeeting(BaseModel):
    model_config = _STRICT

    meeting_id: _Text
    created_at: AwareDatetime
    language: Language
    defect: Defect | None
    sources: tuple[FixtureSource, ...]
    items: tuple[FixtureItem, ...]


class SidedEvidence(BaseModel):
    model_config = _STRICT

    first: tuple[str, ...]
    second: tuple[str, ...]


class LabeledPair(BaseModel):
    model_config = _STRICT

    id: _Text
    first: _Text
    second: _Text
    label: Label
    categories: tuple[Category, ...]
    language: Language
    temporal_order: TemporalOrder
    gold_evidence: SidedEvidence
    rationale: Annotated[str, StringConstraints(min_length=40)]


class LineageDataset(BaseModel):
    model_config = _STRICT

    version: Literal[1]
    label_authority: Literal["curated_evaluation_judgment"]
    synthetic: Literal[True]
    owner_id: _Text
    twin_id: _Text
    guidelines: dict[Label, _Text]
    meetings: tuple[FixtureMeeting, ...]
    pairs: Annotated[tuple[LabeledPair, ...], Field(min_length=80, max_length=120)]


class Prediction(BaseModel):
    model_config = _STRICT

    pair_id: _Text
    label: Label
    evidence: SidedEvidence


class PredictionSet(BaseModel):
    model_config = _STRICT

    predictor: _Text
    predictions: tuple[Prediction, ...]


@dataclass(frozen=True, slots=True)
class PairSide:
    meeting: FixtureMeeting
    item: FixtureItem
    record: DecisionRecord | None

    @property
    def valid_entries(self) -> frozenset[str]:
        if self.record is None:
            return frozenset()
        return frozenset(evidence.entry_id for evidence in self.record.evidence)


@dataclass(frozen=True, slots=True)
class ResolvedPair:
    pair: LabeledPair
    first: PairSide
    second: PairSide


def load_dataset(path: Path = FIXTURE) -> LineageDataset:
    return LineageDataset.model_validate_json(path.read_text(encoding="utf-8"))


def _report(dataset: LineageDataset, meeting: FixtureMeeting) -> DebriefReport:
    return DebriefReport.model_validate(
        {
            "schema_version": "meeting_debrief/v1",
            "owner_id": dataset.owner_id,
            "twin_id": dataset.twin_id,
            "meeting_id": meeting.meeting_id,
            "record_sha256": "0" * 64,
            "context_sha256": None,
            "generated_at": meeting.created_at,
            "provider": "curated",
            "model": "none",
            "source_count": len(meeting.sources),
            "chunk_count": 1,
            "status": "completed",
            "review_status": "unreviewed",
            "items": [item.model_dump(exclude={"key"}) for item in meeting.items],
            "findings": [],
            "evidence_sources": [
                {
                    "id": source.id,
                    "text": source.text,
                    "kind": "participant",
                    "participant_id": source.participant_id,
                    "at": meeting.created_at,
                }
                for source in meeting.sources
            ],
            "source_limitations": [],
        }
    )


def meeting_records(
    dataset: LineageDataset, meeting: FixtureMeeting
) -> dict[str, DecisionRecord | None]:
    scope = MeetingScope(
        owner_id=dataset.owner_id,
        twin_id=dataset.twin_id,
        meeting_id=meeting.meeting_id,
        created_at=meeting.created_at.isoformat(),
    )
    try:
        records = records_from_report(scope, _report(dataset, meeting))
    except DebriefValidationError:
        return dict.fromkeys((item.key for item in meeting.items), None)
    by_index = {record.index: record for record in records}
    return {item.key: by_index.get(index) for index, item in enumerate(meeting.items)}


def resolve_pairs(dataset: LineageDataset) -> tuple[ResolvedPair, ...]:
    sides: dict[str, PairSide] = {}
    for meeting in dataset.meetings:
        records = meeting_records(dataset, meeting)
        for item in meeting.items:
            if item.key in sides:
                raise ValueError(f"item key {item.key} is not unique")
            sides[item.key] = PairSide(meeting, item, records[item.key])
    return tuple(
        ResolvedPair(pair, sides[pair.first], sides[pair.second]) for pair in dataset.pairs
    )


@dataclass(frozen=True, slots=True)
class ClassScore:
    support: int
    predicted: int
    true_positive: int

    @property
    def precision(self) -> float | None:
        return self.true_positive / self.predicted if self.predicted else None

    @property
    def recall(self) -> float | None:
        return self.true_positive / self.support if self.support else None


@dataclass(frozen=True, slots=True)
class LineageScore:
    predictor: str
    total: int
    missing: int
    confusion: Mapping[Label, Mapping[Label, int]]
    classes: Mapping[Label, ClassScore]
    relationship_false_positive_rate: float
    abstention_recall: float
    answered_coverage: float
    evidence_correctness: float | None
    temporal_violations: int
    approval_resolutions: int
    invalid_evidence_answers: int

    def summary(self) -> dict[str, object]:
        document = asdict(self)
        document["confusion"] = {gold: dict(row) for gold, row in self.confusion.items()}
        document["classes"] = {
            label: {
                "support": score.support,
                "predicted": score.predicted,
                "precision": score.precision,
                "recall": score.recall,
            }
            for label, score in self.classes.items()
        }
        return document


def _indexed(predictions: PredictionSet, pairs: Sequence[ResolvedPair]) -> dict[str, Prediction]:
    known = {resolved.pair.id for resolved in pairs}
    indexed: dict[str, Prediction] = {}
    for prediction in predictions.predictions:
        if prediction.pair_id not in known:
            raise ValueError(f"prediction for unknown pair {prediction.pair_id}")
        if prediction.pair_id in indexed:
            raise ValueError(f"pair {prediction.pair_id} is predicted twice")
        indexed[prediction.pair_id] = prediction
    return indexed


def evidence_is_correct(resolved: ResolvedPair, evidence: SidedEvidence) -> bool:
    return (
        bool(evidence.first)
        and bool(evidence.second)
        and set(evidence.first) <= resolved.first.valid_entries
        and set(evidence.second) <= resolved.second.valid_entries
    )


def _share(hits: int, total: int) -> float:
    return hits / total if total else 1.0


def score(predictions: PredictionSet, pairs: Sequence[ResolvedPair]) -> LineageScore:
    indexed = _indexed(predictions, pairs)
    abstained = SidedEvidence(first=(), second=())
    confusion: dict[Label, dict[Label, int]] = {gold: dict.fromkeys(LABELS, 0) for gold in LABELS}
    answered: list[tuple[ResolvedPair, Prediction]] = []
    for resolved in pairs:
        prediction = indexed.get(resolved.pair.id) or Prediction(
            pair_id=resolved.pair.id, label="abstain", evidence=abstained
        )
        confusion[resolved.pair.label][prediction.label] += 1
        if prediction.label != "abstain":
            answered.append((resolved, prediction))
    gold_counts = Counter(resolved.pair.label for resolved in pairs)
    classes = {
        label: ClassScore(
            support=gold_counts[label],
            predicted=sum(row[label] for row in confusion.values()),
            true_positive=confusion[label][label],
        )
        for label in LABELS
    }
    none_row = confusion["no_established_relationship"]
    answerable = len(pairs) - gold_counts["abstain"]
    return LineageScore(
        predictor=predictions.predictor,
        total=len(pairs),
        missing=len(pairs) - len(indexed),
        confusion=confusion,
        classes=classes,
        relationship_false_positive_rate=_share(
            sum(none_row[label] for label in RELATIONSHIPS),
            gold_counts["no_established_relationship"],
        ),
        abstention_recall=_share(confusion["abstain"]["abstain"], gold_counts["abstain"]),
        answered_coverage=_share(
            sum(
                count
                for gold, row in confusion.items()
                if gold != "abstain"
                for label, count in row.items()
                if label != "abstain"
            ),
            answerable,
        ),
        evidence_correctness=(
            sum(evidence_is_correct(resolved, item.evidence) for resolved, item in answered)
            / len(answered)
            if answered
            else None
        ),
        temporal_violations=sum(
            resolved.pair.temporal_order != "strictly_earlier"
            for resolved, item in answered
            if item.label == "potential_supersession"
        ),
        approval_resolutions=sum(
            "owner_approval_claim" in resolved.pair.categories
            for resolved, item in answered
            if item.label == "potential_supersession"
        ),
        invalid_evidence_answers=sum(
            resolved.first.record is None or resolved.second.record is None
            for resolved, _ in answered
        ),
    )


@dataclass(frozen=True, slots=True)
class LineageGate:
    minimum_precision: Mapping[Label, float]
    minimum_recall: Mapping[Label, float]
    maximum_relationship_false_positive_rate: float
    minimum_abstention_recall: float
    minimum_answered_coverage: float


GATE = LineageGate(
    minimum_precision={
        "related_discussion": 0.85,
        "potential_contradiction": 0.90,
        "potential_supersession": 0.90,
        "no_established_relationship": 0.80,
    },
    minimum_recall={
        "related_discussion": 0.70,
        "potential_contradiction": 0.70,
        "potential_supersession": 0.70,
        "no_established_relationship": 0.80,
    },
    maximum_relationship_false_positive_rate=0.05,
    minimum_abstention_recall=0.80,
    minimum_answered_coverage=0.80,
)


def _class_failures(result: LineageScore, gate: LineageGate) -> list[str]:
    failures: list[str] = []
    for label, floor in gate.minimum_precision.items():
        precision = result.classes[label].precision
        if precision is None or precision < floor:
            failures.append(f"{label} precision {precision} < {floor}")
    for label, floor in gate.minimum_recall.items():
        recall = result.classes[label].recall
        if recall is None or recall < floor:
            failures.append(f"{label} recall {recall} < {floor}")
    return failures


def gate_failures(result: LineageScore, gate: LineageGate = GATE) -> list[str]:
    failures = _class_failures(result, gate)
    if result.relationship_false_positive_rate > gate.maximum_relationship_false_positive_rate:
        failures.append(
            f"relationship false positive rate {result.relationship_false_positive_rate:.3f} > "
            f"{gate.maximum_relationship_false_positive_rate}"
        )
    if result.abstention_recall < gate.minimum_abstention_recall:
        failures.append(f"abstention recall {result.abstention_recall:.3f}")
    if result.answered_coverage < gate.minimum_answered_coverage:
        failures.append(f"answered coverage {result.answered_coverage:.3f}")
    if result.evidence_correctness is not None and result.evidence_correctness < 1.0:
        failures.append(f"evidence correctness {result.evidence_correctness:.3f} < 1.0")
    for name in ("temporal_violations", "approval_resolutions", "invalid_evidence_answers"):
        if getattr(result, name):
            failures.append(f"{name} {getattr(result, name)} > 0")
    return failures


def _cited(resolved: ResolvedPair) -> SidedEvidence:
    return SidedEvidence(
        first=tuple(sorted(resolved.first.valid_entries)),
        second=tuple(sorted(resolved.second.valid_entries)),
    )


def oracle_predictions(pairs: Sequence[ResolvedPair]) -> PredictionSet:
    return PredictionSet(
        predictor="oracle",
        predictions=tuple(
            Prediction(
                pair_id=resolved.pair.id,
                label=resolved.pair.label,
                evidence=resolved.pair.gold_evidence,
            )
            for resolved in pairs
        ),
    )


def constant_predictions(label: Label, pairs: Sequence[ResolvedPair]) -> PredictionSet:
    return PredictionSet(
        predictor=f"constant:{label}",
        predictions=tuple(
            Prediction(pair_id=resolved.pair.id, label=label, evidence=_cited(resolved))
            for resolved in pairs
        ),
    )


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


def lexical_predictions(pairs: Sequence[ResolvedPair], *, threshold: float = 0.2) -> PredictionSet:
    texts = {
        side.item.key: side.item.text
        for resolved in pairs
        for side in (resolved.first, resolved.second)
    }
    document_frequency = Counter(token for text in texts.values() for token in _tokens(text))

    def weight(token: str) -> float:
        return math.log(len(texts) / document_frequency[token])

    def similarity(first: str, second: str) -> float:
        left, right = _tokens(first), _tokens(second)
        union = sum(weight(token) for token in left | right)
        return sum(weight(token) for token in left & right) / union if union else 0.0

    return PredictionSet(
        predictor=f"lexical_weighted_jaccard>={threshold}",
        predictions=tuple(
            Prediction(
                pair_id=resolved.pair.id,
                label="related_discussion"
                if similarity(resolved.first.item.text, resolved.second.item.text) >= threshold
                else "no_established_relationship",
                evidence=_cited(resolved),
            )
            for resolved in pairs
        ),
    )


BASELINES: dict[str, Callable[[Sequence[ResolvedPair]], PredictionSet]] = {
    "oracle": oracle_predictions,
    "lexical": lexical_predictions,
    **{f"constant:{label}": functools.partial(constant_predictions, label) for label in LABELS},
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals._decision_lineage",
        description="Score decision lineage predictions against the curated offline labels",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--predictions", type=Path)
    source.add_argument("--baseline", choices=sorted(BASELINES))
    parser.add_argument("--dataset", type=Path, default=FIXTURE)
    arguments = parser.parse_args(argv)
    pairs = resolve_pairs(load_dataset(arguments.dataset))
    predictions = (
        PredictionSet.model_validate_json(arguments.predictions.read_text(encoding="utf-8"))
        if arguments.predictions is not None
        else BASELINES[arguments.baseline](pairs)
    )
    result = score(predictions, pairs)
    failures = gate_failures(result)
    sys.stdout.write(
        json.dumps({**result.summary(), "gate_failures": failures}, indent=2, sort_keys=True) + "\n"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
