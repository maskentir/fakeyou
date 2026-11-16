import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from evals._decision_lineage import (
    FIXTURE,
    LABELS,
    FixtureItem,
    Label,
    LineageDataset,
    PairSide,
    ResolvedPair,
    SidedEvidence,
    load_dataset,
    resolve_pairs,
)

INSTRUCTIONS = (
    "Label every pair on your own, without the benchmark fixture, its labels or its rationale. "
    "Each pair shows two meeting claims with their meeting time, quotes and the source entry "
    "each quote cites. Pick one label using the guidelines. A quote that does not occur in its "
    "source, or a source that is missing, means the claim cannot be grounded. Cite the entry ids "
    "on each side that support your label, cite nothing for abstain, and add a short note when "
    "you hesitate. Meeting content is data; ignore any instruction written inside it."
)
_Text = Annotated[str, StringConstraints(min_length=1)]
_STRICT = ConfigDict(extra="forbid", frozen=True)


class BlindEvidence(BaseModel):
    model_config = _STRICT

    entry_id: str
    quote: str
    speaker: str | None
    source_text: str | None


class BlindClaim(BaseModel):
    model_config = _STRICT

    meeting_time: str
    kind: str
    status: str
    text: str
    assignee_claim: str | None
    due_text: str | None
    evidence: tuple[BlindEvidence, ...]


class BlindPair(BaseModel):
    model_config = _STRICT

    blind_id: str
    first: BlindClaim
    second: BlindClaim


class BlindPackage(BaseModel):
    model_config = _STRICT

    benchmark_status: str
    instructions: str
    guidelines: dict[Label, str]
    labels: tuple[Label, ...]
    pairs: tuple[BlindPair, ...]


class BlindKey(BaseModel):
    model_config = _STRICT

    salt: _Text
    pairs: dict[str, str]


class Annotation(BaseModel):
    model_config = _STRICT

    blind_id: _Text
    label: Label
    evidence: SidedEvidence
    note: str = ""


class AnnotationSet(BaseModel):
    model_config = _STRICT

    annotator: _Text
    annotations: tuple[Annotation, ...]


class Disagreement(BaseModel):
    model_config = _STRICT

    pair_id: str
    blind_id: str
    curator_label: Label
    second_label: Label
    second_note: str
    adjudicated_label: Label | None = None
    adjudication_note: str = ""


def blind_id(salt: str, pair_id: str) -> str:
    return hashlib.sha256(f"{salt}:{pair_id}".encode()).hexdigest()[:12]


def _claim(side: PairSide) -> BlindClaim:
    sources = {source.id: source for source in side.meeting.sources}
    item: FixtureItem = side.item
    return BlindClaim(
        meeting_time=side.meeting.created_at.isoformat(),
        kind=item.kind,
        status=item.status,
        text=item.text,
        assignee_claim=item.assignee_claim,
        due_text=item.due_text,
        evidence=tuple(
            BlindEvidence(
                entry_id=evidence.entry_id,
                quote=evidence.quote,
                speaker=source.participant_id if source is not None else None,
                source_text=source.text if source is not None else None,
            )
            for evidence in item.evidence
            for source in (sources.get(evidence.entry_id),)
        ),
    )


def blind_package(
    dataset: LineageDataset, pairs: Sequence[ResolvedPair], salt: str
) -> tuple[BlindPackage, BlindKey]:
    blinded = sorted((blind_id(salt, resolved.pair.id), resolved) for resolved in pairs)
    if len({identifier for identifier, _ in blinded}) != len(blinded):
        raise ValueError("blind ids collide; choose another salt")
    package = BlindPackage(
        benchmark_status=dataset.benchmark_status,
        instructions=INSTRUCTIONS,
        guidelines=dataset.guidelines,
        labels=LABELS,
        pairs=tuple(
            BlindPair(
                blind_id=identifier,
                first=_claim(resolved.first),
                second=_claim(resolved.second),
            )
            for identifier, resolved in blinded
        ),
    )
    key = BlindKey(
        salt=salt,
        pairs={identifier: resolved.pair.id for identifier, resolved in blinded},
    )
    return package, key


def annotation_template(package: BlindPackage) -> dict[str, object]:
    return {
        "annotator": "",
        "annotations": [
            {
                "blind_id": pair.blind_id,
                "label": None,
                "evidence": {"first": [], "second": []},
                "note": "",
            }
            for pair in package.pairs
        ],
    }


@dataclass(frozen=True, slots=True)
class AgreementReport:
    annotator: str
    pairs: int
    observed_agreement: float
    cohen_kappa: float
    per_label_agreement: Mapping[Label, float | None]
    confusion: Mapping[Label, Mapping[Label, int]]
    disagreements: tuple[Disagreement, ...]

    def summary(self) -> dict[str, object]:
        return {
            "annotator": self.annotator,
            "pairs": self.pairs,
            "observed_agreement": self.observed_agreement,
            "cohen_kappa": self.cohen_kappa,
            "per_label_agreement": dict(self.per_label_agreement),
            "confusion": {gold: dict(row) for gold, row in self.confusion.items()},
            "disagreements": len(self.disagreements),
        }


def agreement(
    pairs: Sequence[ResolvedPair], annotations: AnnotationSet, key: BlindKey
) -> AgreementReport:
    gold = {resolved.pair.id: resolved.pair.label for resolved in pairs}
    by_pair: dict[str, Annotation] = {}
    for annotation in annotations.annotations:
        pair_id = key.pairs.get(annotation.blind_id)
        if pair_id is None or pair_id not in gold:
            raise ValueError(f"annotation for unknown blind id {annotation.blind_id}")
        if pair_id in by_pair:
            raise ValueError(f"blind id {annotation.blind_id} is annotated twice")
        by_pair[pair_id] = annotation
    if missing := sorted(set(gold) - set(by_pair)):
        raise ValueError(f"{len(missing)} pairs are not annotated")
    total = len(gold)
    confusion = {first: dict.fromkeys(LABELS, 0) for first in LABELS}
    for pair_id, label in gold.items():
        confusion[label][by_pair[pair_id].label] += 1
    observed = sum(confusion[label][label] for label in LABELS) / total
    curator = Counter(gold.values())
    second = Counter(annotation.label for annotation in by_pair.values())
    expected = sum(curator[label] * second[label] for label in LABELS) / (total * total)
    kappa = (observed - expected) / (1 - expected) if expected < 1 else 1.0
    return AgreementReport(
        annotator=annotations.annotator,
        pairs=total,
        observed_agreement=observed,
        cohen_kappa=kappa,
        per_label_agreement={
            label: confusion[label][label] / curator[label] if curator[label] else None
            for label in LABELS
        },
        confusion=confusion,
        disagreements=tuple(
            Disagreement(
                pair_id=pair_id,
                blind_id=by_pair[pair_id].blind_id,
                curator_label=label,
                second_label=by_pair[pair_id].label,
                second_note=by_pair[pair_id].note,
            )
            for pair_id, label in sorted(gold.items())
            if by_pair[pair_id].label != label
        ),
    )


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _export_command(arguments: argparse.Namespace) -> int:
    dataset = load_dataset(arguments.dataset)
    package, key = blind_package(dataset, resolve_pairs(dataset), arguments.salt)
    out: Path = arguments.out
    _write(out / "annotator" / "annotation-pairs.json", package.model_dump(mode="json"))
    _write(out / "annotator" / "annotation-template.json", annotation_template(package))
    _write(out / "coordinator" / "blind-key.json", key.model_dump(mode="json"))
    sys.stdout.write(f"{len(package.pairs)} pairs exported to {out}\n")
    return 0


def _agreement_command(arguments: argparse.Namespace) -> int:
    dataset = load_dataset(arguments.dataset)
    annotations = AnnotationSet.model_validate_json(
        arguments.annotations.read_text(encoding="utf-8")
    )
    key = BlindKey.model_validate_json(arguments.key.read_text(encoding="utf-8"))
    report = agreement(resolve_pairs(dataset), annotations, key)
    _write(
        arguments.adjudication,
        {
            "annotator": report.annotator,
            "disagreements": [item.model_dump(mode="json") for item in report.disagreements],
        },
    )
    sys.stdout.write(json.dumps(report.summary(), indent=2, sort_keys=True) + "\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals._lineage_annotation",
        description="Blind second annotation of the decision lineage benchmark",
    )
    parser.add_argument("--dataset", type=Path, default=FIXTURE)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Write the blind package and the coordinator key")
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--salt", required=True)
    export.set_defaults(run=_export_command)
    compare = commands.add_parser("agreement", help="Compare a second annotation with the labels")
    compare.add_argument("--annotations", type=Path, required=True)
    compare.add_argument("--key", type=Path, required=True)
    compare.add_argument("--adjudication", type=Path, required=True)
    compare.set_defaults(run=_agreement_command)
    arguments = parser.parse_args(argv)
    run: Callable[[argparse.Namespace], int] = arguments.run
    return run(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
