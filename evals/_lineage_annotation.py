import argparse
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from evals._decision_lineage import (
    FIXTURE,
    LABELS,
    FixtureItem,
    Label,
    LabelCorrection,
    LineageDataset,
    PairSide,
    ResolvedPair,
    SidedEvidence,
    load_dataset,
    resolve_pairs,
)

EXAMPLES = Path(__file__).parent / "fixtures" / "decision-lineage-annotation-examples.json"
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


class ExampleClaim(BaseModel):
    model_config = _STRICT

    meeting_time: str
    text: _Text


class WorkedExample(BaseModel):
    model_config = _STRICT

    first: ExampleClaim
    second: ExampleClaim
    label: Label
    why: Annotated[str, StringConstraints(min_length=40)]


class WorkedExamples(BaseModel):
    model_config = _STRICT

    version: Literal[1]
    note: _Text
    examples: tuple[WorkedExample, ...]


def load_examples(path: Path = EXAMPLES) -> WorkedExamples:
    return WorkedExamples.model_validate_json(path.read_text(encoding="utf-8"))


class BlindPackage(BaseModel):
    model_config = _STRICT

    benchmark_status: str
    instructions: str
    guidelines: dict[Label, str]
    labels: tuple[Label, ...]
    worked_examples: tuple[WorkedExample, ...]
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
        worked_examples=load_examples().examples,
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
            "disagreement_types": dict(
                sorted(
                    Counter(
                        f"{item.curator_label} -> {item.second_label}"
                        for item in self.disagreements
                    ).items()
                )
            ),
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


def package_leaks(dataset: LineageDataset, package: BlindPackage, key: BlindKey) -> list[str]:
    exported = package.model_dump_json()
    leaks = [f"pair id {pair.id}" for pair in dataset.pairs if pair.id in exported]
    leaks += [
        f"rationale of {pair.id}" for pair in dataset.pairs if pair.rationale[:60] in exported
    ]
    leaks += [
        f"claim key {item.key}"
        for meeting in dataset.meetings
        for item in meeting.items
        if f'"{item.key}"' in exported
    ]
    leaks += [
        f"meeting id {meeting.meeting_id}"
        for meeting in dataset.meetings
        if meeting.meeting_id in exported
    ]
    leaks += [
        f'field "{name}"'
        for name in ("categories", "gold_evidence", "temporal_order", "defect", "rationale")
        if f'"{name}"' in exported
    ]
    leaks += ["coordinator salt"] if key.salt in exported else []
    texts = {
        value
        for meeting in dataset.meetings
        for value in (
            *(item.text for item in meeting.items),
            *(source.text for source in meeting.sources),
        )
    }
    leaks += [
        f"worked example reuses benchmark text: {claim.text[:40]}"
        for example in package.worked_examples
        for claim in (example.first, example.second)
        if claim.text in texts
    ]
    return leaks


class Adjudication(BaseModel):
    model_config = _STRICT

    annotator: _Text
    disagreements: tuple[Disagreement, ...]


def proposed_corrections(adjudication: Adjudication) -> list[LabelCorrection]:
    pending = [
        item.pair_id for item in adjudication.disagreements if item.adjudicated_label is None
    ]
    if pending:
        raise ValueError(f"{len(pending)} disagreements are not adjudicated yet")
    return [
        LabelCorrection(
            pair_id=item.pair_id,
            previous_label=item.curator_label,
            corrected_label=item.adjudicated_label,
            reason=item.adjudication_note,
        )
        for item in adjudication.disagreements
        if item.adjudicated_label is not None and item.adjudicated_label != item.curator_label
    ]


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _export_command(arguments: argparse.Namespace) -> int:
    dataset = load_dataset(arguments.dataset)
    package, key = blind_package(dataset, resolve_pairs(dataset), arguments.salt)
    leaks = package_leaks(dataset, package, key)
    if leaks:
        sys.stdout.write(
            json.dumps({"refused": "package leaks curation", "leaks": leaks}, indent=2) + "\n"
        )
        return 1
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


def _verify_command(arguments: argparse.Namespace) -> int:
    dataset = load_dataset(arguments.dataset)
    folder: Path = arguments.package
    package = BlindPackage.model_validate_json(
        (folder / "annotator" / "annotation-pairs.json").read_text(encoding="utf-8")
    )
    key = BlindKey.model_validate_json(
        (folder / "coordinator" / "blind-key.json").read_text(encoding="utf-8")
    )
    leaks = package_leaks(dataset, package, key)
    annotator_files = sorted(path.name for path in (folder / "annotator").iterdir())
    report = {
        "pairs": len(package.pairs),
        "worked_examples": len(package.worked_examples),
        "annotator_files": annotator_files,
        "coordinator_key_in_annotator_folder": "blind-key.json" in annotator_files,
        "leaks": leaks,
    }
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 1 if leaks or report["coordinator_key_in_annotator_folder"] else 0


def _corrections_command(arguments: argparse.Namespace) -> int:
    adjudication = Adjudication.model_validate_json(
        arguments.adjudication.read_text(encoding="utf-8")
    )
    corrections = proposed_corrections(adjudication)
    sys.stdout.write(
        json.dumps(
            {
                "applies_automatically": False,
                "proposed_label_corrections": [
                    item.model_dump(mode="json") for item in corrections
                ],
            },
            indent=2,
        )
        + "\n"
    )
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
    verify = commands.add_parser("verify", help="Check an exported package for leaked curation")
    verify.add_argument("--package", type=Path, required=True)
    verify.set_defaults(run=_verify_command)
    corrections = commands.add_parser(
        "corrections", help="Propose label corrections from a finished adjudication; edits nothing"
    )
    corrections.add_argument("--adjudication", type=Path, required=True)
    corrections.set_defaults(run=_corrections_command)
    arguments = parser.parse_args(argv)
    run: Callable[[argparse.Namespace], int] = arguments.run
    return run(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
