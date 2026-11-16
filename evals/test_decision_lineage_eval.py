import json
from collections import Counter
from pathlib import Path
from typing import get_args

import pytest

import fakeyou.debrief.lineage as lineage_module
from evals._decision_lineage import (
    BASELINES,
    GATE,
    LABELS,
    RELATIONSHIPS,
    Category,
    LabeledPair,
    Language,
    LineageDataset,
    Prediction,
    PredictionSet,
    ResolvedPair,
    SidedEvidence,
    candidate_retrieval,
    constant_predictions,
    gate_failures,
    lexical_predictions,
    load_dataset,
    main,
    oracle_labels_citing_everything,
    oracle_labels_with_wrong_evidence,
    oracle_predictions,
    resolve_pairs,
    score,
    selects_evidence,
)


@pytest.fixture(scope="module")
def dataset() -> LineageDataset:
    return load_dataset()


@pytest.fixture(scope="module")
def pairs(dataset: LineageDataset) -> tuple[ResolvedPair, ...]:
    return resolve_pairs(dataset)


def test_dataset_is_synthetic_curated_and_scoped_to_one_owner_and_twin(
    dataset: LineageDataset,
) -> None:
    assert dataset.benchmark_status == (
        "synthetic / single-curator / provisional evaluation benchmark"
    )
    assert "second annotator" in dataset.provenance
    assert dataset.label_corrections == ()
    assert dataset.synthetic is True
    assert dataset.label_authority == "curated_evaluation_judgment"
    assert set(dataset.guidelines) == set(LABELS)
    assert (dataset.owner_id, dataset.twin_id) == ("eval_owner", "eval_twin")


def test_dataset_covers_every_label_language_and_hard_case(dataset: LineageDataset) -> None:
    labels = Counter(pair.label for pair in dataset.pairs)
    languages = Counter(pair.language for pair in dataset.pairs)
    categories = Counter(category for pair in dataset.pairs for category in pair.categories)

    assert 80 <= len(dataset.pairs) <= 130
    assert all(labels[label] >= 15 for label in LABELS), labels
    assert all(languages[language] >= 15 for language in get_args(Language)), languages
    assert set(get_args(Category)) <= set(categories), categories
    for category in (
        "paraphrase",
        "hard_negative",
        "owner_approval_claim",
        "equal_timestamps",
        "prompt_injection",
    ):
        assert categories[category] >= 3, category


def test_pair_ids_and_pairs_are_unique(dataset: LineageDataset) -> None:
    ids = [pair.id for pair in dataset.pairs]
    combinations = [(pair.first, pair.second) for pair in dataset.pairs]

    assert len(ids) == len(set(ids))
    assert len(combinations) == len(set(combinations))


def test_each_pair_spans_two_meetings_in_their_recorded_time_order(
    pairs: tuple[ResolvedPair, ...],
) -> None:
    for resolved in pairs:
        first, second = resolved.first.meeting, resolved.second.meeting
        assert first.meeting_id != second.meeting_id, resolved.pair.id
        assert first.created_at <= second.created_at, resolved.pair.id
        expected = "strictly_earlier" if first.created_at < second.created_at else "equal"
        assert resolved.pair.temporal_order == expected, resolved.pair.id
        assert ("equal_timestamps" in resolved.pair.categories) == (expected == "equal")


def test_supersession_needs_a_strict_order_and_never_settles_approval(
    dataset: LineageDataset,
) -> None:
    for pair in dataset.pairs:
        if pair.label == "potential_supersession":
            assert pair.temporal_order == "strictly_earlier", pair.id
            assert "owner_approval_claim" not in pair.categories, pair.id


def test_claims_resolve_through_the_dm1_register_unless_their_evidence_is_broken(
    pairs: tuple[ResolvedPair, ...],
) -> None:
    for resolved in pairs:
        for side in (resolved.first, resolved.second):
            assert (side.record is None) == (side.meeting.defect is not None), side.item.key
            if side.record is not None:
                assert side.record.text == side.item.text
                assert all(
                    evidence.quote
                    in next(
                        source.text
                        for source in side.meeting.sources
                        if source.id == evidence.entry_id
                    )
                    for evidence in side.record.evidence
                )


def test_broken_evidence_is_always_labelled_abstain(pairs: tuple[ResolvedPair, ...]) -> None:
    broken = [
        resolved
        for resolved in pairs
        if resolved.first.record is None or resolved.second.record is None
    ]

    assert len(broken) >= 5
    for resolved in broken:
        assert resolved.pair.label == "abstain", resolved.pair.id
        assert {"altered_evidence", "missing_evidence"} & set(resolved.pair.categories)


def test_gold_evidence_cites_only_the_records_own_entries(
    pairs: tuple[ResolvedPair, ...],
) -> None:
    for resolved in pairs:
        gold = resolved.pair.gold_evidence
        if resolved.pair.label == "abstain":
            assert gold == SidedEvidence(first=(), second=()), resolved.pair.id
        else:
            assert gold.first, resolved.pair.id
            assert gold.second, resolved.pair.id
            assert set(gold.first) <= resolved.first.valid_entries
            assert set(gold.second) <= resolved.second.valid_entries


def test_the_oracle_scores_perfectly_and_passes_the_gate(
    pairs: tuple[ResolvedPair, ...],
) -> None:
    result = score(oracle_predictions(pairs), pairs)

    assert gate_failures(result) == []
    assert all(row[gold] == sum(row.values()) for gold, row in result.confusion.items())
    assert result.evidence_validity == 1.0
    assert result.missing == 0


@pytest.mark.parametrize("label", LABELS)
def test_a_constant_predictor_never_passes(label: str, pairs: tuple[ResolvedPair, ...]) -> None:
    assert gate_failures(score(BASELINES[f"constant:{label}"](pairs), pairs))


def test_constant_supersession_is_caught_by_the_temporal_and_approval_rules(
    pairs: tuple[ResolvedPair, ...],
) -> None:
    result = score(constant_predictions("potential_supersession", pairs), pairs)

    assert result.temporal_violations == sum(
        resolved.pair.temporal_order == "equal" for resolved in pairs
    )
    assert result.approval_resolutions == sum(
        "owner_approval_claim" in resolved.pair.categories for resolved in pairs
    )
    assert result.invalid_evidence_answers == sum(
        resolved.first.record is None or resolved.second.record is None for resolved in pairs
    )


def test_the_lexical_baseline_cannot_separate_conflict_from_discussion(
    pairs: tuple[ResolvedPair, ...],
) -> None:
    result = score(lexical_predictions(pairs), pairs)
    failures = gate_failures(result)

    assert result.classes["potential_contradiction"].recall == 0
    assert result.classes["potential_supersession"].recall == 0
    assert result.abstention_recall == 0
    assert result.relationship_false_positive_rate > GATE.maximum_relationship_false_positive_rate
    assert len(failures) >= 5


def _single(pair_id: str, label: str, evidence: SidedEvidence) -> PredictionSet:
    return PredictionSet.model_validate(
        {
            "predictor": "probe",
            "predictions": [
                {"pair_id": pair_id, "label": label, "evidence": evidence.model_dump()}
            ],
        }
    )


def _first_with(pairs: tuple[ResolvedPair, ...], label: str) -> ResolvedPair:
    return next(resolved for resolved in pairs if resolved.pair.label == label)


def test_a_missing_prediction_counts_as_abstention(pairs: tuple[ResolvedPair, ...]) -> None:
    resolved = _first_with(pairs, "potential_contradiction")
    result = score(
        _single(resolved.pair.id, "potential_contradiction", resolved.pair.gold_evidence),
        pairs,
    )

    assert result.missing == len(pairs) - 1
    assert result.confusion["potential_contradiction"]["potential_contradiction"] == 1
    assert result.classes["potential_contradiction"].precision == 1.0
    assert result.abstention_recall == 1.0
    assert result.evidence_validity == 1.0


def test_evidence_from_the_wrong_side_or_an_empty_side_is_incorrect(
    pairs: tuple[ResolvedPair, ...],
) -> None:
    resolved = _first_with(pairs, "related_discussion")
    gold = resolved.pair.gold_evidence
    swapped = SidedEvidence(first=gold.second + ("transcript:e99",), second=gold.first)

    for evidence in (swapped, SidedEvidence(first=gold.first, second=())):
        result = score(_single(resolved.pair.id, "related_discussion", evidence), pairs)
        assert result.evidence_validity == 0.0
        assert "evidence validity 0.000 < 1.0" in gate_failures(result)


def test_unknown_and_repeated_predictions_are_refused(pairs: tuple[ResolvedPair, ...]) -> None:
    empty = SidedEvidence(first=(), second=())
    repeated = PredictionSet(
        predictor="probe",
        predictions=(
            Prediction(pair_id=pairs[0].pair.id, label="abstain", evidence=empty),
            Prediction(pair_id=pairs[0].pair.id, label="abstain", evidence=empty),
        ),
    )

    with pytest.raises(ValueError, match="unknown pair"):
        score(_single("pair-999", "abstain", empty), pairs)
    with pytest.raises(ValueError, match="predicted twice"):
        score(repeated, pairs)


def test_a_rationale_too_short_to_review_is_refused(dataset: LineageDataset) -> None:
    pair = dataset.pairs[0].model_dump()

    with pytest.raises(ValueError, match="at least 40 characters"):
        LabeledPair.model_validate(pair | {"rationale": "Same subject."})


def test_the_runner_scores_a_predictions_file_and_reports_gate_failures(
    pairs: tuple[ResolvedPair, ...], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    perfect = tmp_path / "oracle.json"
    perfect.write_text(oracle_predictions(pairs).model_dump_json(), encoding="utf-8")

    assert main(["score", "--predictions", str(perfect)]) == 0
    passed = json.loads(capsys.readouterr().out)
    assert main(["score", "--baseline", "lexical"]) == 1
    failed = json.loads(capsys.readouterr().out)

    assert passed["gate_failures"] == []
    assert passed["predictor"] == "oracle"
    assert failed["predictor"].startswith("lexical")
    assert failed["gate_failures"]
    assert set(failed["confusion"]) == set(LABELS)


def test_multi_evidence_pairs_select_a_strict_subset_of_valid_evidence(
    pairs: tuple[ResolvedPair, ...],
) -> None:
    selective = [resolved for resolved in pairs if selects_evidence(resolved)]

    assert len(selective) >= 8
    for resolved in selective:
        assert "multi_evidence" in resolved.pair.categories
        gold = resolved.pair.gold_evidence
        cited = len(gold.first) + len(gold.second)
        available = len(resolved.first.valid_entries) + len(resolved.second.valid_entries)
        assert cited < available, resolved.pair.id


def test_right_labels_with_wrong_evidence_fail_the_selection_gate(
    pairs: tuple[ResolvedPair, ...],
) -> None:
    result = score(oracle_labels_with_wrong_evidence(pairs), pairs)
    failures = gate_failures(result)

    assert result.evidence_validity == 1.0
    assert result.evidence_selection_sufficiency == 0.0
    assert result.evidence_selection_precision == 0.5
    assert all(result.classes[label].recall == 1.0 for label in LABELS)
    assert any(failure.startswith("evidence selection sufficiency") for failure in failures)


def test_citing_every_valid_entry_fails_the_selection_precision_gate(
    pairs: tuple[ResolvedPair, ...],
) -> None:
    result = score(oracle_labels_citing_everything(pairs), pairs)
    failures = gate_failures(result)

    assert result.evidence_validity == 1.0
    assert result.evidence_selection_sufficiency == 1.0
    assert result.evidence_selection_precision is not None
    assert result.evidence_selection_precision < GATE.minimum_selection_precision
    assert [failure.split(" ")[:3] for failure in failures] == [
        ["evidence", "selection", "precision"]
    ]


def test_selection_is_scored_only_where_a_choice_exists(pairs: tuple[ResolvedPair, ...]) -> None:
    result = score(oracle_predictions(pairs), pairs)

    assert result.selection_pairs == sum(selects_evidence(resolved) for resolved in pairs)
    assert result.evidence_selection_sufficiency == 1.0
    assert result.evidence_selection_precision == 1.0


def test_candidate_retrieval_stays_inside_the_dm2a_window_and_never_reads_labels(
    dataset: LineageDataset, pairs: tuple[ResolvedPair, ...]
) -> None:
    result = candidate_retrieval(dataset, pairs)
    relabelled = [
        ResolvedPair(
            resolved.pair.model_copy(update={"rationale": "x" * 40}),
            resolved.first,
            resolved.second,
        )
        for resolved in pairs
    ]

    assert result.positive_pairs == sum(resolved.pair.label in RELATIONSHIPS for resolved in pairs)
    assert result.excluded["not_strictly_earlier"] == tuple(
        resolved.pair.id
        for resolved in pairs
        if resolved.pair.label in RELATIONSHIPS and resolved.pair.temporal_order == "equal"
    )
    assert result.eligible_pairs == result.positive_pairs - sum(
        len(ids) for ids in result.excluded.values()
    )
    assert (result.eligible_pairs, result.hits[5], result.hits[10]) == (83, 75, 81)
    assert result.misses[10] == ("pair-036", "pair-112")
    assert candidate_retrieval(dataset, relabelled) == result


def test_the_window_bound_excludes_pairs_beyond_it(
    dataset: LineageDataset, pairs: tuple[ResolvedPair, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lineage_module, "WINDOW_MEETINGS", 1)

    result = candidate_retrieval(dataset, pairs)

    assert result.excluded["outside_window"]
    assert result.eligible_pairs < 83


def test_the_retrieval_command_reports_recall_and_exclusions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["retrieval"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["benchmark_status"].startswith("synthetic / single-curator")
    assert report["eligible_pairs"] == 83
    assert set(report["recall"]) == {"at_5", "at_10"}
    assert report["excluded"]["not_strictly_earlier"]
