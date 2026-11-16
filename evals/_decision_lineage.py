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
from fakeyou.debrief.lineage import comparison_window
from fakeyou.debrief.register import (
    DecisionRecord,
    DecisionRegister,
    MeetingCoverage,
    MeetingScope,
    records_from_report,
)

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
    "multi_evidence",
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


class LabelCorrection(BaseModel):
    model_config = _STRICT

    pair_id: _Text
    previous_label: Label
    corrected_label: Label
    reason: Annotated[str, StringConstraints(min_length=40)]


class LineageDataset(BaseModel):
    model_config = _STRICT

    version: Literal[1]
    label_authority: Literal["curated_evaluation_judgment"]
    synthetic: Literal[True]
    benchmark_status: Literal["synthetic / single-curator / provisional evaluation benchmark"]
    provenance: Annotated[str, StringConstraints(min_length=80)]
    label_corrections: tuple[LabelCorrection, ...]
    owner_id: _Text
    twin_id: _Text
    guidelines: dict[Label, _Text]
    meetings: tuple[FixtureMeeting, ...]
    pairs: Annotated[tuple[LabeledPair, ...], Field(min_length=80, max_length=130)]


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
    evidence_validity: float | None
    selection_pairs: int
    evidence_selection_sufficiency: float | None
    evidence_selection_precision: float | None
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


def evidence_is_valid(resolved: ResolvedPair, evidence: SidedEvidence) -> bool:
    return (
        bool(evidence.first)
        and bool(evidence.second)
        and set(evidence.first) <= resolved.first.valid_entries
        and set(evidence.second) <= resolved.second.valid_entries
    )


def _share(hits: int, total: int) -> float:
    return hits / total if total else 1.0


def selects_evidence(resolved: ResolvedPair) -> bool:
    return resolved.pair.label != "abstain" and (
        len(resolved.first.valid_entries) > 1 or len(resolved.second.valid_entries) > 1
    )


@dataclass(frozen=True, slots=True)
class SelectionScore:
    pairs: int
    sufficiency: float | None
    precision: float | None


def evidence_selection(answered: Sequence[tuple[ResolvedPair, "Prediction"]]) -> SelectionScore:
    judged = [(resolved, item) for resolved, item in answered if selects_evidence(resolved)]
    if not judged:
        return SelectionScore(0, None, None)
    sufficient = cited = relevant = 0
    for resolved, item in judged:
        gold = resolved.pair.gold_evidence
        sides = ((gold.first, item.evidence.first), (gold.second, item.evidence.second))
        sufficient += all(set(expected) <= set(given) for expected, given in sides)
        cited += sum(len(set(given)) for _, given in sides)
        relevant += sum(len(set(given) & set(expected)) for expected, given in sides)
    return SelectionScore(
        pairs=len(judged),
        sufficiency=sufficient / len(judged),
        precision=relevant / cited if cited else 0.0,
    )


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
    selection = evidence_selection(answered)
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
        evidence_validity=(
            sum(evidence_is_valid(resolved, item.evidence) for resolved, item in answered)
            / len(answered)
            if answered
            else None
        ),
        selection_pairs=selection.pairs,
        evidence_selection_sufficiency=selection.sufficiency,
        evidence_selection_precision=selection.precision,
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
    minimum_selection_sufficiency: float
    minimum_selection_precision: float


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
    minimum_selection_sufficiency=0.90,
    minimum_selection_precision=0.90,
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


def _evidence_failures(result: LineageScore, gate: LineageGate) -> list[str]:
    failures: list[str] = []
    if result.evidence_validity is not None and result.evidence_validity < 1.0:
        failures.append(f"evidence validity {result.evidence_validity:.3f} < 1.0")
    sufficiency = result.evidence_selection_sufficiency
    if sufficiency is not None and sufficiency < gate.minimum_selection_sufficiency:
        failures.append(
            f"evidence selection sufficiency {sufficiency:.3f} < "
            f"{gate.minimum_selection_sufficiency}"
        )
    precision = result.evidence_selection_precision
    if precision is not None and precision < gate.minimum_selection_precision:
        failures.append(
            f"evidence selection precision {precision:.3f} < {gate.minimum_selection_precision}"
        )
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
    failures.extend(_evidence_failures(result, gate))
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


def oracle_labels_with_wrong_evidence(pairs: Sequence[ResolvedPair]) -> PredictionSet:
    def wrong(resolved: ResolvedPair) -> SidedEvidence:
        gold = resolved.pair.gold_evidence
        if resolved.pair.label == "abstain":
            return gold
        first = tuple(sorted(resolved.first.valid_entries - set(gold.first))) or gold.first
        second = tuple(sorted(resolved.second.valid_entries - set(gold.second))) or gold.second
        return SidedEvidence(first=first, second=second)

    return PredictionSet(
        predictor="oracle_labels_with_wrong_evidence",
        predictions=tuple(
            Prediction(
                pair_id=resolved.pair.id, label=resolved.pair.label, evidence=wrong(resolved)
            )
            for resolved in pairs
        ),
    )


def oracle_labels_citing_everything(pairs: Sequence[ResolvedPair]) -> PredictionSet:
    return PredictionSet(
        predictor="oracle_labels_citing_everything",
        predictions=tuple(
            Prediction(
                pair_id=resolved.pair.id,
                label=resolved.pair.label,
                evidence=resolved.pair.gold_evidence
                if resolved.pair.label == "abstain"
                else _cited(resolved),
            )
            for resolved in pairs
        ),
    )


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


class LexicalIndex:
    def __init__(self, texts: Sequence[str]) -> None:
        self._size = len(texts)
        self._frequency = Counter(token for text in texts for token in _tokens(text))

    def _weight(self, token: str) -> float:
        return math.log(self._size / self._frequency[token]) if self._frequency[token] else 0.0

    def similarity(self, first: str, second: str) -> float:
        left, right = _tokens(first), _tokens(second)
        union = sum(self._weight(token) for token in left | right)
        return sum(self._weight(token) for token in left & right) / union if union else 0.0


def lexical_predictions(pairs: Sequence[ResolvedPair], *, threshold: float = 0.2) -> PredictionSet:
    texts = {
        side.item.key: side.item.text
        for resolved in pairs
        for side in (resolved.first, resolved.second)
    }
    index = LexicalIndex(list(texts.values()))
    return PredictionSet(
        predictor=f"lexical_weighted_jaccard>={threshold}",
        predictions=tuple(
            Prediction(
                pair_id=resolved.pair.id,
                label="related_discussion"
                if index.similarity(resolved.first.item.text, resolved.second.item.text)
                >= threshold
                else "no_established_relationship",
                evidence=_cited(resolved),
            )
            for resolved in pairs
        ),
    )


BASELINES: dict[str, Callable[[Sequence[ResolvedPair]], PredictionSet]] = {
    "oracle": oracle_predictions,
    "oracle_labels_with_wrong_evidence": oracle_labels_with_wrong_evidence,
    "oracle_labels_citing_everything": oracle_labels_citing_everything,
    "lexical": lexical_predictions,
    **{f"constant:{label}": functools.partial(constant_predictions, label) for label in LABELS},
}

RetrievalExclusion = Literal["broken_evidence", "not_strictly_earlier", "outside_window"]
RETRIEVAL_DEPTHS = (5, 10)


def dataset_register(dataset: LineageDataset) -> DecisionRegister:
    records: list[DecisionRecord] = []
    meetings: list[MeetingCoverage] = []
    for meeting in sorted(dataset.meetings, key=lambda item: (item.created_at, item.meeting_id)):
        found = [record for record in meeting_records(dataset, meeting).values() if record]
        scope = MeetingScope(
            owner_id=dataset.owner_id,
            twin_id=dataset.twin_id,
            meeting_id=meeting.meeting_id,
            created_at=meeting.created_at.isoformat(),
        )
        meetings.append(MeetingCoverage(scope, "available" if found else "invalid", len(found), ()))
        records.extend(found)
    return DecisionRegister(tuple(records), tuple(meetings))


def retrieval_text(record: DecisionRecord) -> str:
    return " ".join([record.text, *(evidence.quote for evidence in record.evidence)])


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    positive_pairs: int
    eligible_pairs: int
    excluded: Mapping[RetrievalExclusion, tuple[str, ...]]
    hits: Mapping[int, int]
    misses: Mapping[int, tuple[str, ...]]

    def recall(self, depth: int) -> float | None:
        return self.hits[depth] / self.eligible_pairs if self.eligible_pairs else None

    def summary(self) -> dict[str, object]:
        return {
            "positive_pairs": self.positive_pairs,
            "eligible_pairs": self.eligible_pairs,
            "excluded": {reason: list(ids) for reason, ids in self.excluded.items()},
            "recall": {f"at_{depth}": self.recall(depth) for depth in RETRIEVAL_DEPTHS},
            "hits": {f"at_{depth}": self.hits[depth] for depth in RETRIEVAL_DEPTHS},
            "misses": {f"at_{depth}": list(self.misses[depth]) for depth in RETRIEVAL_DEPTHS},
        }


def _exclusion(
    resolved: ResolvedPair, windows: Callable[[DecisionRecord], frozenset[str]]
) -> RetrievalExclusion | None:
    earlier, later = resolved.first.record, resolved.second.record
    if earlier is None or later is None:
        return "broken_evidence"
    if resolved.pair.temporal_order != "strictly_earlier":
        return "not_strictly_earlier"
    if earlier.record_id not in windows(later):
        return "outside_window"
    return None


def candidate_retrieval(dataset: LineageDataset, pairs: Sequence[ResolvedPair]) -> RetrievalResult:
    register = dataset_register(dataset)
    by_id = {record.record_id: record for record in register.records}
    index = LexicalIndex([retrieval_text(record) for record in register.records])
    cached: dict[str, frozenset[str]] = {}

    def window(later: DecisionRecord) -> frozenset[str]:
        if later.scope.meeting_id not in cached:
            cached[later.scope.meeting_id] = comparison_window(later.scope, register)
        return cached[later.scope.meeting_id]

    excluded: dict[RetrievalExclusion, list[str]] = {
        reason: [] for reason in get_args(RetrievalExclusion)
    }
    hits = dict.fromkeys(RETRIEVAL_DEPTHS, 0)
    misses: dict[int, list[str]] = {depth: [] for depth in RETRIEVAL_DEPTHS}
    positives = [resolved for resolved in pairs if resolved.pair.label in RELATIONSHIPS]
    for resolved in positives:
        reason = _exclusion(resolved, window)
        if reason is not None:
            excluded[reason].append(resolved.pair.id)
            continue
        earlier, later = resolved.first.record, resolved.second.record
        if earlier is None or later is None:
            continue
        ranked = sorted(
            window(later),
            key=lambda record_id: (
                -index.similarity(retrieval_text(later), retrieval_text(by_id[record_id])),
                record_id,
            ),
        )
        rank = ranked.index(earlier.record_id)
        for depth in RETRIEVAL_DEPTHS:
            if rank < depth:
                hits[depth] += 1
            else:
                misses[depth].append(resolved.pair.id)
    return RetrievalResult(
        positive_pairs=len(positives),
        eligible_pairs=len(positives) - sum(len(ids) for ids in excluded.values()),
        excluded={reason: tuple(ids) for reason, ids in excluded.items()},
        hits=hits,
        misses={depth: tuple(ids) for depth, ids in misses.items()},
    )


def _score_command(arguments: argparse.Namespace) -> int:
    dataset = load_dataset(arguments.dataset)
    pairs = resolve_pairs(dataset)
    predictions = (
        PredictionSet.model_validate_json(arguments.predictions.read_text(encoding="utf-8"))
        if arguments.predictions is not None
        else BASELINES[arguments.baseline](pairs)
    )
    result = score(predictions, pairs)
    failures = gate_failures(result)
    report = {
        **result.summary(),
        "benchmark_status": dataset.benchmark_status,
        "gate_failures": failures,
    }
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 1 if failures else 0


def _retrieval_command(arguments: argparse.Namespace) -> int:
    dataset = load_dataset(arguments.dataset)
    result = candidate_retrieval(dataset, resolve_pairs(dataset))
    report = {**result.summary(), "benchmark_status": dataset.benchmark_status}
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals._decision_lineage",
        description="Offline decision lineage benchmark: scoring and candidate retrieval",
    )
    parser.add_argument("--dataset", type=Path, default=FIXTURE)
    commands = parser.add_subparsers(dest="command", required=True)
    scoring = commands.add_parser("score", help="Score predictions against the curated labels")
    source = scoring.add_mutually_exclusive_group(required=True)
    source.add_argument("--predictions", type=Path)
    source.add_argument("--baseline", choices=sorted(BASELINES))
    scoring.set_defaults(run=_score_command)
    retrieval = commands.add_parser(
        "retrieval", help="Recall of lexical candidate ranking inside the DM-2a window"
    )
    retrieval.set_defaults(run=_retrieval_command)
    arguments = parser.parse_args(argv)
    run: Callable[[argparse.Namespace], int] = arguments.run
    return run(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
