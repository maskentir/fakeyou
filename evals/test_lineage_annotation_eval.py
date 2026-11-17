import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals._decision_lineage import (
    FIXTURE,
    LABELS,
    LineageDataset,
    ResolvedPair,
    load_dataset,
    resolve_pairs,
)
from evals._lineage_annotation import (
    Adjudication,
    Annotation,
    AnnotationSet,
    BlindKey,
    Disagreement,
    agreement,
    annotation_template,
    blind_package,
    main,
    package_leaks,
    proposed_corrections,
)


@pytest.fixture(scope="module")
def dataset() -> LineageDataset:
    return load_dataset()


@pytest.fixture(scope="module")
def pairs(dataset: LineageDataset) -> tuple[ResolvedPair, ...]:
    return resolve_pairs(dataset)


def annotate(
    key: BlindKey, pairs: tuple[ResolvedPair, ...], label_of: Mapping[str, str]
) -> AnnotationSet:
    by_pair = {resolved.pair.id: resolved for resolved in pairs}
    return AnnotationSet.model_validate(
        {
            "annotator": "second",
            "annotations": [
                {
                    "blind_id": identifier,
                    "label": label_of[pair_id],
                    "evidence": by_pair[pair_id].pair.gold_evidence.model_dump(),
                    "note": "",
                }
                for identifier, pair_id in key.pairs.items()
            ],
        }
    )


def test_the_blind_package_carries_claims_but_no_curation(
    dataset: LineageDataset, pairs: tuple[ResolvedPair, ...]
) -> None:
    package, key = blind_package(dataset, pairs, "salt-a")
    exported = package.model_dump_json()

    assert len(package.pairs) == len(pairs) == len(key.pairs)
    for pair in dataset.pairs:
        assert pair.id not in exported
        assert pair.rationale not in exported
        assert pair.first not in key.pairs
    for meeting in dataset.meetings:
        assert meeting.meeting_id not in exported
    for forbidden in ('"categories"', '"gold_evidence"', '"temporal_order"', '"defect"'):
        assert forbidden not in exported
    assert package.benchmark_status == dataset.benchmark_status
    assert set(package.guidelines) == set(dataset.guidelines)


def test_the_package_shows_broken_evidence_as_it_is(
    dataset: LineageDataset, pairs: tuple[ResolvedPair, ...]
) -> None:
    package, key = blind_package(dataset, pairs, "salt-a")
    blinded = {key.pairs[pair.blind_id]: pair for pair in package.pairs}

    missing = [
        evidence
        for resolved in pairs
        if "missing_evidence" in resolved.pair.categories
        for evidence in blinded[resolved.pair.id].second.evidence
    ]
    altered = [
        evidence
        for resolved in pairs
        if "altered_evidence" in resolved.pair.categories
        for evidence in blinded[resolved.pair.id].second.evidence
    ]
    assert missing
    assert all(evidence.source_text is None for evidence in missing)
    assert altered
    assert all(
        evidence.source_text is not None and evidence.quote not in evidence.source_text
        for evidence in altered
    )


def test_the_salt_decides_ids_and_order_reproducibly(
    dataset: LineageDataset, pairs: tuple[ResolvedPair, ...]
) -> None:
    first, first_key = blind_package(dataset, pairs, "salt-a")
    again, again_key = blind_package(dataset, pairs, "salt-a")
    other, other_key = blind_package(dataset, pairs, "salt-b")

    assert first == again
    assert first_key == again_key
    assert set(first_key.pairs.values()) == set(other_key.pairs.values())
    assert [pair.blind_id for pair in first.pairs] != [pair.blind_id for pair in other.pairs]


def test_an_unfilled_template_cannot_be_scored(
    dataset: LineageDataset, pairs: tuple[ResolvedPair, ...]
) -> None:
    package, _ = blind_package(dataset, pairs, "salt-a")

    with pytest.raises(ValidationError):
        AnnotationSet.model_validate(annotation_template(package) | {"annotator": "second"})


def test_identical_labels_agree_fully(
    dataset: LineageDataset, pairs: tuple[ResolvedPair, ...]
) -> None:
    _, key = blind_package(dataset, pairs, "salt-a")
    gold = {resolved.pair.id: resolved.pair.label for resolved in pairs}

    report = agreement(pairs, annotate(key, pairs, gold), key)

    assert report.observed_agreement == 1.0
    assert report.cohen_kappa == 1.0
    assert report.disagreements == ()


def test_a_constant_annotator_has_no_agreement_beyond_chance(
    dataset: LineageDataset, pairs: tuple[ResolvedPair, ...]
) -> None:
    _, key = blind_package(dataset, pairs, "salt-a")
    constant = {resolved.pair.id: "related_discussion" for resolved in pairs}

    report = agreement(pairs, annotate(key, pairs, constant), key)

    assert report.cohen_kappa == pytest.approx(0.0)
    assert len(report.disagreements) == sum(
        resolved.pair.label != "related_discussion" for resolved in pairs
    )
    assert all(item.adjudicated_label is None for item in report.disagreements)
    assert report.per_label_agreement["related_discussion"] == 1.0


def test_incomplete_unknown_or_repeated_annotations_are_refused(
    dataset: LineageDataset, pairs: tuple[ResolvedPair, ...]
) -> None:
    _, key = blind_package(dataset, pairs, "salt-a")
    gold = {resolved.pair.id: resolved.pair.label for resolved in pairs}
    complete = annotate(key, pairs, gold)
    first = complete.annotations[0]
    stranger = Annotation.model_validate(first.model_dump() | {"blind_id": "000000000000"})

    for annotations, message in (
        (complete.annotations[1:], "pairs are not annotated"),
        ((*complete.annotations, stranger), "unknown blind id"),
        ((*complete.annotations, first), "annotated twice"),
    ):
        with pytest.raises(ValueError, match=message):
            agreement(pairs, complete.model_copy(update={"annotations": annotations}), key)


def test_export_then_agreement_round_trip_writes_an_adjudication_file(
    pairs: tuple[ResolvedPair, ...], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["export", "--out", str(tmp_path / "package"), "--salt", "salt-a"]) == 0
    capsys.readouterr()
    key = BlindKey.model_validate_json(
        (tmp_path / "package" / "coordinator" / "blind-key.json").read_text(encoding="utf-8")
    )
    template = json.loads(
        (tmp_path / "package" / "annotator" / "annotation-template.json").read_text(
            encoding="utf-8"
        )
    )
    labels = {resolved.pair.id: resolved.pair.label for resolved in pairs}
    first_pair = sorted(labels)[0]
    labels[first_pair] = "abstain" if labels[first_pair] != "abstain" else "related_discussion"
    annotations = tmp_path / "second.json"
    annotations.write_text(annotate(key, pairs, labels).model_dump_json(), encoding="utf-8")

    assert (
        main(
            [
                "agreement",
                "--annotations",
                str(annotations),
                "--key",
                str(tmp_path / "package" / "coordinator" / "blind-key.json"),
                "--adjudication",
                str(tmp_path / "adjudication.json"),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    adjudication = json.loads((tmp_path / "adjudication.json").read_text(encoding="utf-8"))

    assert len(template["annotations"]) == len(pairs)
    assert report["disagreements"] == 1
    assert report["observed_agreement"] == pytest.approx((len(pairs) - 1) / len(pairs))
    assert [item["pair_id"] for item in adjudication["disagreements"]] == [first_pair]
    assert adjudication["disagreements"][0]["adjudicated_label"] is None


def test_the_package_teaches_with_examples_that_are_not_scored(
    dataset: LineageDataset, pairs: tuple[ResolvedPair, ...]
) -> None:
    package, key = blind_package(dataset, pairs, "salt-a")

    assert len(package.worked_examples) >= 5
    assert {example.label for example in package.worked_examples} == set(LABELS)
    assert package_leaks(dataset, package, key) == []


def test_the_leak_check_catches_curation_in_the_package(
    dataset: LineageDataset, pairs: tuple[ResolvedPair, ...]
) -> None:
    package, key = blind_package(dataset, pairs, "salt-a")
    leaked = package.model_copy(
        update={"instructions": f"{package.instructions} {dataset.pairs[0].id} {key.salt}"}
    )

    assert package_leaks(dataset, leaked, key) == [
        f"pair id {dataset.pairs[0].id}",
        "coordinator salt",
    ]


def test_verify_accepts_a_clean_export_and_rejects_a_misplaced_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = tmp_path / "package"
    assert main(["export", "--out", str(folder), "--salt", "salt-a"]) == 0
    assert main(["verify", "--package", str(folder)]) == 0
    clean = json.loads(capsys.readouterr().out.split("\n", 1)[1])
    (folder / "annotator" / "blind-key.json").write_text(
        (folder / "coordinator" / "blind-key.json").read_text(encoding="utf-8"), encoding="utf-8"
    )

    assert main(["verify", "--package", str(folder)]) == 1
    assert clean["leaks"] == []
    assert clean["coordinator_key_in_annotator_folder"] is False


def test_corrections_need_a_finished_adjudication_and_a_written_reason() -> None:
    pending = Adjudication(
        annotator="second",
        disagreements=(
            Disagreement(
                pair_id="pair-001",
                blind_id="abc",
                curator_label="potential_contradiction",
                second_label="potential_supersession",
                second_note="",
            ),
        ),
    )
    agreed = pending.model_copy(
        update={
            "disagreements": (
                pending.disagreements[0].model_copy(
                    update={"adjudicated_label": "potential_contradiction"}
                ),
            )
        }
    )
    unexplained = pending.model_copy(
        update={
            "disagreements": (
                pending.disagreements[0].model_copy(
                    update={"adjudicated_label": "potential_supersession"}
                ),
            )
        }
    )

    with pytest.raises(ValueError, match="not adjudicated"):
        proposed_corrections(pending)
    assert proposed_corrections(agreed) == []
    with pytest.raises(ValueError, match="at least 40 characters"):
        proposed_corrections(unexplained)


def test_proposed_corrections_never_touch_the_benchmark(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = FIXTURE.read_bytes()
    adjudication = tmp_path / "adjudication.json"
    adjudication.write_text(
        json.dumps(
            {
                "annotator": "second",
                "disagreements": [
                    {
                        "pair_id": "pair-001",
                        "blind_id": "abc",
                        "curator_label": "potential_contradiction",
                        "second_label": "potential_supersession",
                        "second_note": "",
                        "adjudicated_label": "potential_supersession",
                        "adjudication_note": (
                            "The later claim says 'for now', which the panel read as a change."
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert main(["corrections", "--adjudication", str(adjudication)]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["applies_automatically"] is False
    assert report["proposed_label_corrections"][0]["corrected_label"] == "potential_supersession"
    assert FIXTURE.read_bytes() == before
