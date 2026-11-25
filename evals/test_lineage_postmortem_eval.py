import json
import socket
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import evals._lineage_postmortem as postmortem
from evals._decision_lineage import (
    Label,
    Prediction,
    PredictionSet,
    SidedEvidence,
    evidence_is_valid,
    load_split,
    partition_claims,
    score,
)
from evals._lineage_classifier import PreparedCall, validate_output
from evals._lineage_postmortem import (
    PILOT_1,
    SEMANTIC_PAIRS,
    AdjudicationResponse,
    Pilot,
    adjudication_packet,
    compare,
    component_calls,
    component_pairs,
    component_suite,
    load_manifest,
    load_pilot,
    packet_leaks,
    rescore,
    retrieval_diagnosis,
    unlabeled_relationships,
    verify_artifacts,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("a provider call was attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(scope="module")
def pilot() -> Pilot:
    return load_pilot()


@pytest.fixture(scope="module")
def report(pilot: Pilot) -> Any:
    return rescore(pilot)


def call_and_ref(pilot: Pilot, pair_id: str) -> tuple[PreparedCall, str, str]:
    trace = next(item for item in pilot.traces if item.resolved.pair.id == pair_id)
    earlier = trace.resolved.first.record
    assert earlier is not None
    for candidate in pilot.candidates:
        if (
            candidate.call.record_for(candidate.ref).record_id == earlier.record_id
            and candidate.call.later == trace.resolved.second.record
        ):
            raw = pilot.provider.respond(candidate.call.request, "gemini-3.8-flash").response
            return candidate.call, candidate.ref, raw
    raise AssertionError(f"{pair_id} was not presented")


def test_the_committed_copies_match_the_recorded_digests_and_originals_never_differ() -> None:
    manifest = load_manifest()
    status = verify_artifacts(manifest)
    original = json.loads((PILOT_1 / "original-report.json").read_text(encoding="utf-8"))

    assert status["cassette"]["committed"] == status["original_report"]["committed"] == "verified"
    assert all(item["original"] in {"verified", "absent"} for item in status.values())
    assert list(manifest.original_gate_failures) == original["gate_failures"]
    assert len(manifest.original_gate_failures) == 5
    assert manifest.execution["attempts"] == 21
    assert manifest.execution["billing_reconciled"] is False


def test_a_tampered_committed_copy_is_refused(tmp_path: Path) -> None:
    for name in ("manifest.json", "cassette.jsonl", "original-report.json"):
        (tmp_path / name).write_bytes((PILOT_1 / name).read_bytes())
    with (tmp_path / "cassette.jsonl").open("a", encoding="utf-8") as cassette:
        cassette.write("\n")

    with pytest.raises(ValueError, match="cassette"):
        verify_artifacts(load_manifest(tmp_path), tmp_path, tmp_path)


def test_validator_v1_reproduces_the_original_result_exactly(report: Any) -> None:
    end_to_end = report["end_to_end"]

    assert end_to_end["original_reproduced"] is True
    assert end_to_end["v1"]["gate_failures"] == end_to_end["original_gate_failures"]
    assert report["post_validation"]["v1"]["evidence_invalid"] == 75


@pytest.mark.parametrize("pair_id", ["pair-010", "pair-034"])
def test_a_negative_answer_without_relationship_evidence_survives_v2(
    pilot: Pilot, pair_id: str
) -> None:
    call, ref, raw = call_and_ref(pilot, pair_id)

    before = validate_output(call, raw, "v1")[ref]
    after = validate_output(call, raw, "v2")[ref]

    assert (before.label, before.reason) == ("abstain", "evidence_invalid")
    assert (after.label, after.reason) == ("no_established_relationship", None)
    assert after.evidence == SidedEvidence(first=(), second=())
    assert after.source == (call.record_for(ref).record_id, call.later.record_id)


def test_v2_still_rejects_fabricated_cross_side_and_partial_evidence(pilot: Pilot) -> None:
    call, ref, _ = call_and_ref(pilot, "pair-010")
    own_earlier = call.record_for(ref).evidence[0].entry_id
    own_later = call.later.evidence[0].entry_id
    earlier_ids = {item.entry_id for item in call.record_for(ref).evidence}
    later_ids = {item.entry_id for item in call.later.evidence}
    foreign = next(
        entry.entry_id
        for record in call.candidates
        for entry in record.evidence
        if entry.entry_id not in earlier_ids
    )
    cross_side = sorted(later_ids - earlier_ids)

    def decide(label: str, earlier: list[str], later: list[str]) -> tuple[str, str | None]:
        raw = json.dumps(
            {
                "comparisons": [
                    {
                        "candidate": ref,
                        "label": label,
                        "earlier_evidence": earlier,
                        "later_evidence": later,
                    }
                ]
            }
        )
        decision = validate_output(call, raw, "v2")[ref]
        return decision.label, decision.reason

    invalid = ("abstain", "evidence_invalid")
    assert decide("no_established_relationship", ["transcript:e999"], []) == invalid
    assert cross_side
    assert decide("no_established_relationship", cross_side[:1], [own_later]) == invalid
    assert decide("no_established_relationship", [foreign], [own_later]) == invalid
    assert decide("no_established_relationship", [own_earlier], []) == invalid
    assert decide("no_established_relationship", [own_earlier], [own_later]) == (
        "no_established_relationship",
        None,
    )
    for label in ("related_discussion", "potential_contradiction", "potential_supersession"):
        assert decide(label, [], []) == invalid
        assert decide(label, [own_earlier], []) == invalid
        assert decide(label, [foreign], [own_later]) == invalid
    assert decide("abstain", [own_earlier], [own_later]) == ("abstain", "model_abstained")


def test_the_scorer_accepts_an_uncited_negative_but_not_a_cited_relationship_without_evidence(
    pilot: Pilot,
) -> None:
    resolved = next(
        trace.resolved for trace in pilot.traces if trace.resolved.pair.id == "pair-010"
    )
    empty = SidedEvidence(first=(), second=())
    wrong = SidedEvidence(first=("transcript:e999",), second=())

    def valid(label: Label, evidence: SidedEvidence) -> bool:
        return evidence_is_valid(
            resolved, Prediction(pair_id=resolved.pair.id, label=label, evidence=evidence)
        )

    assert valid("no_established_relationship", empty)
    assert not valid("no_established_relationship", wrong)
    assert not valid("related_discussion", empty)
    result = score(
        PredictionSet(
            predictor="negative",
            predictions=(
                Prediction(
                    pair_id=resolved.pair.id, label="no_established_relationship", evidence=empty
                ),
            ),
        ),
        (resolved,),
    )
    assert result.evidence_validity == 1.0
    assert result.selection_pairs == 0


def test_model_level_denominators_exclude_pairs_the_model_never_saw(report: Any) -> None:
    stages = report["stage_counts"]
    model = report["model_level"]

    assert stages == {
        "broken_evidence": 1,
        "no_strict_time_order": 1,
        "presented": 17,
        "retrieval_miss": 4,
    }
    assert report["stages"]["retrieval_miss"] == ["pair-052", "pair-064", "pair-065", "pair-120"]
    for name in ("raw", "validated_v1", "validated_v2"):
        assert model[name]["denominator"] == 17
    assert (model["raw"]["correct"], model["validated_v1"]["correct"]) == (13, 11)
    assert model["validated_v2"]["correct"] == 13
    assert model["raw"]["contradiction_as_supersession"] == 3
    assert model["raw"]["per_class"]["abstain"]["support"] == 1


def test_the_corrected_score_is_labelled_and_keeps_the_genuine_failures(report: Any) -> None:
    corrected = report["end_to_end"]["v2"]

    assert "not a new measurement" in corrected["notice"]
    assert corrected["gate_failures"] == [
        "related_discussion precision 0.8 < 0.85",
        "potential_supersession precision 0.5714285714285714 < 0.9",
        "potential_contradiction recall 0.2 < 0.7",
    ]
    assert report["post_validation"]["v2"].get("evidence_invalid", 0) == 0


def test_retrieval_misses_are_explained_by_missing_lexical_overlap(pilot: Pilot) -> None:
    misses: dict[str, Any] = {str(item["pair"]): item for item in retrieval_diagnosis(pilot)}

    assert set(misses) == {"pair-052", "pair-064", "pair-065", "pair-120"}
    for item in misses.values():
        assert item["rank"] > 10
        assert item["similarity"] <= item["tenth_similarity"]
    assert all(misses[name]["shared_tokens"] == [] for name in ("pair-052", "pair-064", "pair-065"))
    assert misses["pair-120"]["shared_tokens"] == ["the"]


def test_six_unlabeled_relationships_are_held_for_annotation(pilot: Pilot, report: Any) -> None:
    unlabeled = unlabeled_relationships(pilot)

    assert len(unlabeled) == 6
    assert {item["status"] for item in report["unlabeled_relationships"]} == {
        "pending human annotation; not counted as correct or incorrect"
    }


def test_the_adjudication_packet_is_blind_and_complete(pilot: Pilot) -> None:
    packet, key = adjudication_packet(pilot)
    text = packet.model_dump_json()

    assert packet_leaks(packet, key) == []
    assert len(packet.items) == 10
    assert sorted(entry.pair_id for entry in key.entries if entry.pair_id) == sorted(SEMANTIC_PAIRS)
    assert sum(entry.source == "unlabeled_candidate" for entry in key.entries) == 6
    for name in ("classifier", "pair-", "gold", "pilot", "benchmark_pair", "unlabeled"):
        assert name not in text
    assert "explicit evidence of revision" in packet.instructions
    for item in packet.items:
        for claim in (item.earlier, item.later):
            assert all(quote.quote in quote.source_line for quote in claim.quotes)


def _complete_responses(
    pilot: Pilot, adjudicator: str, overrides: dict[str, Label] | None = None
) -> list[AdjudicationResponse]:
    packet, key = adjudication_packet(pilot)
    items = {item.item: item for item in packet.items}
    responses = []
    for entry in key.entries:
        label: Label = (overrides or {}).get(entry.pair_id or "", "abstain")
        evidence: tuple[str, ...] = ()
        if label == "potential_supersession":
            evidence = (items[entry.item].later.quotes[0].entry_id,)
        responses.append(
            AdjudicationResponse(
                item=entry.item,
                adjudicator=adjudicator,
                label=label,
                revision_evidence=evidence,
                rationale="Judged from the quoted claims shown for this item alone.",
            )
        )
    return responses


def test_adjudication_records_disagreements_without_touching_gold(pilot: Pilot) -> None:
    packet, key = adjudication_packet(pilot)
    target = next(entry for entry in key.entries if entry.pair_id == "pair-001")

    result: Any = compare(
        packet,
        key,
        _complete_responses(pilot, "reviewer-a", {"pair-001": "potential_supersession"}),
    )

    assert result["gold_changes_applied"] is False
    assert result["second_independent_annotator"] == "pending"
    assert result["disagreements_with_gold"][0]["pair_id"] == "pair-001"
    with pytest.raises(ValidationError):
        AdjudicationResponse(
            item=target.item, adjudicator="a", label="abstain", revision_evidence=(), rationale="x"
        )
    with pytest.raises(ValueError, match="unknown items"):
        compare(
            packet,
            key,
            [
                response.model_copy(update={"item": "item-99"})
                for response in _complete_responses(pilot, "reviewer-a")
            ],
        )


def test_compare_rejects_duplicate_and_incomplete_answers(pilot: Pilot) -> None:
    packet, key = adjudication_packet(pilot)
    complete = _complete_responses(pilot, "reviewer-a")

    with pytest.raises(ValueError, match="answered more than once"):
        compare(packet, key, [*complete, complete[0]])
    with pytest.raises(ValueError, match="did not answer every item"):
        compare(packet, key, complete[:-1])


def test_compare_requires_later_revision_evidence_for_supersession(pilot: Pilot) -> None:
    packet, key = adjudication_packet(pilot)
    target = next(entry for entry in key.entries if entry.pair_id == "pair-053")
    item = next(item for item in packet.items if item.item == target.item)
    complete = _complete_responses(pilot, "reviewer-a", {"pair-053": "potential_supersession"})
    superseded = next(response for response in complete if response.item == target.item)

    def swap(evidence: tuple[str, ...]) -> list[AdjudicationResponse]:
        return [
            response.model_copy(update={"revision_evidence": evidence})
            if response.item == target.item
            else response
            for response in complete
        ]

    with pytest.raises(ValueError, match="revision evidence"):
        compare(packet, key, swap(()))
    with pytest.raises(ValueError, match="later claim"):
        compare(packet, key, swap((item.earlier.quotes[0].entry_id,)))

    result: Any = compare(packet, key, complete)

    row = next(row for row in result["rows"] if row["item"] == target.item)
    assert row["revision_evidence"] == [item.later.quotes[0].entry_id]
    assert superseded.revision_evidence == (item.later.quotes[0].entry_id,)
    assert row["revision_evidence_quotes"] == [item.later.quotes[0].quote]


def test_compare_resolves_shared_entry_id_to_later_quote_text(pilot: Pilot) -> None:
    packet, key = adjudication_packet(pilot)
    target = next(entry for entry in key.entries if entry.pair_id == "pair-001")
    item = next(item for item in packet.items if item.item == target.item)
    assert item.earlier.quotes[0].entry_id == item.later.quotes[0].entry_id
    assert item.earlier.quotes[0].quote != item.later.quotes[0].quote

    result: Any = compare(
        packet,
        key,
        _complete_responses(pilot, "reviewer-a", {"pair-001": "potential_supersession"}),
    )

    row = next(row for row in result["rows"] if row["item"] == target.item)
    assert row["revision_evidence_quotes"] == [item.later.quotes[0].quote]


def test_compare_agreement_counts_only_overlapping_items(pilot: Pilot) -> None:
    packet, key = adjudication_packet(pilot)
    first = _complete_responses(pilot, "reviewer-a", {"pair-001": "potential_supersession"})
    second = _complete_responses(pilot, "reviewer-b")

    alone: Any = compare(packet, key, first)
    assert alone["second_independent_annotator"] == "pending"
    assert alone["inter_annotator_agreement"]["status"] == "unavailable"
    assert alone["gold_agreement"]["by_adjudicator"]["reviewer-a"]["total"] == 4
    assert len(alone["first_annotations"]) == 6

    both: Any = compare(packet, key, [*first, *second])
    assert both["second_independent_annotator"] == "present"
    agreement = both["inter_annotator_agreement"]
    assert agreement["status"] == "available"
    assert agreement["overlapping_items"] == [entry.item for entry in key.entries]
    assert agreement["pairs"][0]["matches"] == 9


def test_compare_rejects_a_swapped_key_mapping(pilot: Pilot) -> None:
    packet, key = adjudication_packet(pilot)
    first, second = key.entries[0], key.entries[1]
    assert first.pair_id != second.pair_id
    swapped = [
        entry.model_copy(update={"item": second.item})
        if entry.item == first.item
        else (entry.model_copy(update={"item": first.item}) if entry.item == second.item else entry)
        for entry in key.entries
    ]
    swapped_key = key.model_copy(update={"entries": tuple(swapped)})
    answers = [
        AdjudicationResponse(
            item=entry.item,
            adjudicator="reviewer-a",
            label="abstain",
            revision_evidence=(),
            rationale="Judged from the quoted claims shown for this item alone.",
        )
        for entry in swapped_key.entries
    ]

    with pytest.raises(ValueError, match="canonical key"):
        compare(packet, swapped_key, answers)


def test_compare_rejects_duplicate_item_ids(pilot: Pilot) -> None:
    packet, key = adjudication_packet(pilot)
    complete = _complete_responses(pilot, "reviewer-a")
    doubled_packet = packet.model_copy(update={"items": (*packet.items, packet.items[0])})
    doubled_key = key.model_copy(update={"entries": (*key.entries, key.entries[0])})

    with pytest.raises(ValueError, match="duplicate item"):
        compare(doubled_packet, key, complete)
    with pytest.raises(ValueError, match="duplicate item"):
        compare(packet, doubled_key, complete)


def test_compare_trims_annotator_names_before_deduplication(pilot: Pilot) -> None:
    packet, key = adjudication_packet(pilot)
    first = _complete_responses(pilot, "reviewer-a")
    padded = [response.model_copy(update={"adjudicator": "reviewer-a "}) for response in first]

    with pytest.raises(ValueError, match="answered more than once"):
        compare(packet, key, [*first, *padded])

    blank = [first[0].model_copy(update={"adjudicator": " "}), *first[1:]]
    with pytest.raises(ValueError, match="blank adjudicator"):
        compare(packet, key, blank)

    with pytest.raises(ValidationError, match="adjudicator"):
        AdjudicationResponse(
            item="item-01",
            adjudicator=" ",
            label="abstain",
            revision_evidence=(),
            rationale="Judged from the quoted claims shown for this item alone.",
        )
    with pytest.raises(ValidationError, match="rationale"):
        AdjudicationResponse(
            item="item-01",
            adjudicator="reviewer-a",
            label="abstain",
            revision_evidence=(),
            rationale=" " * 40,
        )


def test_compare_two_annotators_with_one_disagreement_scores_nine_of_ten(
    pilot: Pilot,
) -> None:
    packet, key = adjudication_packet(pilot)
    first = _complete_responses(pilot, "reviewer-a")
    second = _complete_responses(pilot, "reviewer-b")
    changed = [
        response.model_copy(update={"label": "potential_contradiction"})
        if response.item == second[0].item
        else response
        for response in second
    ]

    result: Any = compare(packet, key, [*first, *changed])

    pair = result["inter_annotator_agreement"]["pairs"][0]
    assert pair["items"] == 10
    assert pair["matches"] == 9


def test_compare_rejects_a_packet_missing_a_labeled_item(pilot: Pilot) -> None:
    packet, key = adjudication_packet(pilot)
    labeled = next(entry for entry in key.entries if entry.gold is not None)
    short_packet = packet.model_copy(
        update={"items": tuple(item for item in packet.items if item.item != labeled.item)}
    )
    short_key = key.model_copy(
        update={"entries": tuple(entry for entry in key.entries if entry.item != labeled.item)}
    )
    answers = [
        response
        for response in _complete_responses(pilot, "reviewer-a")
        if response.item != labeled.item
    ]

    with pytest.raises(ValueError, match="canonical"):
        compare(short_packet, short_key, answers)


def test_compare_rejects_a_packet_missing_an_unlabeled_candidate(pilot: Pilot) -> None:
    packet, key = adjudication_packet(pilot)
    candidate = next(entry for entry in key.entries if entry.gold is None)
    short_packet = packet.model_copy(
        update={"items": tuple(item for item in packet.items if item.item != candidate.item)}
    )
    short_key = key.model_copy(
        update={"entries": tuple(entry for entry in key.entries if entry.item != candidate.item)}
    )
    answers = [
        response
        for response in _complete_responses(pilot, "reviewer-a")
        if response.item != candidate.item
    ]

    with pytest.raises(ValueError, match="canonical"):
        compare(short_packet, short_key, answers)


def test_compare_rejects_non_canonical_instructions_and_labels(pilot: Pilot) -> None:
    packet, key = adjudication_packet(pilot)
    complete = _complete_responses(pilot, "reviewer-a")
    blank_instructions = packet.model_copy(update={"instructions": ""})
    trimmed_labels = packet.model_copy(
        update={"labels": tuple(label for label in packet.labels if label != "abstain")}
    )

    with pytest.raises(ValueError, match="canonical"):
        compare(blank_instructions, key, complete)
    with pytest.raises(ValueError, match="canonical"):
        compare(trimmed_labels, key, complete)


@pytest.mark.parametrize("payload", ["null", "true"])
def test_compare_cli_refuses_non_array_responses(
    pilot: Pilot, tmp_path: Path, capsys: pytest.CaptureFixture[str], payload: str
) -> None:
    packet, key = adjudication_packet(pilot)
    packet_path = tmp_path / "packet.json"
    key_path = tmp_path / "key.json"
    responses_path = tmp_path / "responses.json"
    packet_path.write_text(packet.model_dump_json(), encoding="utf-8")
    key_path.write_text(key.model_dump_json(), encoding="utf-8")
    responses_path.write_text(payload, encoding="utf-8")

    code = postmortem.main(
        [
            "compare",
            "--packet",
            str(packet_path),
            "--key",
            str(key_path),
            "--responses",
            str(responses_path),
        ]
    )

    assert code == 2
    assert "array" in json.loads(capsys.readouterr().out)["refused"]


def test_the_component_suite_forces_each_case_and_stays_in_development(pilot: Pilot) -> None:
    suite: Any = component_suite(pilot)
    development = partition_claims(load_split(), "development")
    chosen = {resolved.pair.id: resolved for resolved in component_pairs(pilot.dataset, pilot)}
    calls = component_calls(pilot.dataset, list(chosen.values()))
    presented = {
        (record.record_id, call.later.record_id) for call in calls for record in call.candidates
    }

    assert suite["suite"] == "classifier_component"
    assert suite["provider_calls_authorized"] is False
    assert suite["categories"]["owner_approval_claim"] == 0
    assert suite["gaps"]
    assert {"pair-120", "pair-052", "pair-064", "pair-065"} <= set(suite["pairs"])
    for resolved in chosen.values():
        assert {resolved.pair.first, resolved.pair.second} <= development
        assert resolved.first.record is not None
        assert resolved.second.record is not None
        assert (resolved.first.record.record_id, resolved.second.record.record_id) in presented
    assert all(1 <= len(call.candidates) <= 10 for call in calls)


def test_the_cli_writes_the_packet_and_key_apart(tmp_path: Path) -> None:
    assert (
        postmortem.main(
            ["packet", "--out", str(tmp_path / "packet"), "--key-out", str(tmp_path / "key")]
        )
        == 0
    )
    assert (tmp_path / "packet" / "packet.json").exists()
    assert not (tmp_path / "packet" / "key.json").exists()
    assert (tmp_path / "key" / "key.json").exists()


@pytest.mark.parametrize("key_folder", [".", "coordinator", "nested/deeper"])
def test_a_key_inside_the_package_folder_publishes_nothing(
    tmp_path: Path, key_folder: str, capsys: pytest.CaptureFixture[str]
) -> None:
    package = tmp_path / "package"

    code = postmortem.main(
        ["packet", "--out", str(package), "--key-out", str(package / key_folder)]
    )

    assert code == 2
    assert "inside it" in json.loads(capsys.readouterr().out)["refused"]
    assert not package.exists()
    assert list(tmp_path.rglob("*.json")) == []


def test_a_key_beside_the_package_folder_is_accepted(tmp_path: Path) -> None:
    assert not postmortem.key_inside_package(tmp_path / "package", tmp_path / "package-key")
    assert postmortem.key_inside_package(
        tmp_path / "package", tmp_path / "package" / ".." / "package"
    )
