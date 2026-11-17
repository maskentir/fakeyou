import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from evals._decision_lineage import (
    PARTITIONS,
    LineageDataset,
    Partition,
    ResolvedPair,
    load_dataset,
    load_split,
    meeting_records,
    partition_claims,
    partition_pairs,
    resolve_pairs,
)
from evals._lineage_classifier import (
    SYSTEM_PROMPT,
    CassetteEntry,
    CassetteMissError,
    ClassifierRequest,
    PreparedCall,
    ReplayProvider,
    RunCaps,
    end_to_end,
    load_plan,
    main,
    partition_scope,
    pilot_scope,
    plan_report,
    planned_calls,
    replay_report,
    run_pipeline,
    validate_output,
)

Answer = Callable[[PreparedCall], list[dict[str, object]]]
Scope = tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]]


@pytest.fixture(scope="module")
def dataset() -> LineageDataset:
    return load_dataset()


@pytest.fixture(scope="module")
def pairs(dataset: LineageDataset) -> tuple[ResolvedPair, ...]:
    return resolve_pairs(dataset)


@pytest.fixture(scope="module")
def scopes(dataset: LineageDataset) -> dict[Partition, Scope]:
    return {partition: partition_scope(dataset, partition) for partition in PARTITIONS}


@pytest.fixture(scope="module")
def calls(scopes: dict[Partition, Scope]) -> tuple[PreparedCall, ...]:
    return tuple(call for _, partition_calls in scopes.values() for call in partition_calls)


@pytest.fixture(scope="module")
def frozen_calls(scopes: dict[Partition, Scope]) -> tuple[PreparedCall, ...]:
    return scopes["frozen_evaluation"][1]


def gold_by_records(pairs: tuple[ResolvedPair, ...]) -> dict[tuple[str, str], ResolvedPair]:
    return {
        (resolved.first.record.record_id, resolved.second.record.record_id): resolved
        for resolved in pairs
        if resolved.first.record is not None and resolved.second.record is not None
    }


def oracle_answer(pairs: tuple[ResolvedPair, ...]) -> Answer:
    gold = gold_by_records(pairs)

    def answer(call: PreparedCall) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for view in call.request.candidates:
            resolved = gold.get((call.record_for(view.ref).record_id, call.later.record_id))
            label = resolved.pair.label if resolved is not None else "no_established_relationship"
            evidence = resolved.pair.gold_evidence if resolved is not None else None
            first = (
                list(evidence.first)
                if evidence
                else [call.record_for(view.ref).evidence[0].entry_id]
            )
            second = list(evidence.second) if evidence else [call.later.evidence[0].entry_id]
            out.append(
                {
                    "candidate": view.ref,
                    "label": label,
                    "earlier_evidence": [] if label == "abstain" else first,
                    "later_evidence": [] if label == "abstain" else second,
                }
            )
        return out

    return answer


@dataclass
class FakeProvider:
    answer: Answer
    calls: list[PreparedCall]
    model: str = "fake-model"
    requests: list[ClassifierRequest] = field(default_factory=list)

    def respond(self, request: ClassifierRequest, model: str) -> CassetteEntry:
        self.requests.append(request)
        call = next(item for item in self.calls if item.request == request)
        return CassetteEntry(
            request_digest=request.digest(model),
            model=model,
            prompt_version="lineage-classifier/v1",
            response=json.dumps({"comparisons": self.answer(call)}),
            input_tokens=1200,
            output_tokens=300,
        )


def generous_caps() -> RunCaps:
    return RunCaps(
        max_calls=200,
        max_input_tokens=10**7,
        max_output_tokens=10**7,
        max_output_tokens_per_call=4096,
        max_spend_usd=100.0,
    )


def first_call_with(calls: tuple[PreparedCall, ...], kind: str) -> PreparedCall:
    return next(
        call
        for call in calls
        if call.later.kind == kind or any(record.kind == kind for record in call.candidates)
    )


def claim_keys(dataset: LineageDataset) -> dict[str, str]:
    return {
        record.record_id: key
        for meeting in dataset.meetings
        for key, record in meeting_records(dataset, meeting).items()
        if record is not None
    }


def test_every_partition_draws_candidates_only_from_its_own_claims(
    dataset: LineageDataset, scopes: dict[Partition, Scope]
) -> None:
    keys = claim_keys(dataset)
    split = load_split()

    for partition, (_, partition_calls) in scopes.items():
        own = partition_claims(split, partition)
        assert partition_calls
        for call in partition_calls:
            assert keys[call.later.record_id] in own
            assert {keys[record.record_id] for record in call.candidates} <= own


def test_frozen_text_never_reaches_a_development_request(
    dataset: LineageDataset, scopes: dict[Partition, Scope]
) -> None:
    frozen = partition_claims(load_split(), "frozen_evaluation")
    development = " ".join(call.request.model_dump_json() for call in scopes["development"][1])
    frozen_items = [
        item for meeting in dataset.meetings for item in meeting.items if item.key in frozen
    ]

    assert frozen_items
    for item in frozen_items:
        assert item.text not in development
        for evidence in item.evidence:
            assert evidence.quote not in development


def test_rewriting_the_frozen_partition_leaves_development_requests_unchanged(
    dataset: LineageDataset, scopes: dict[Partition, Scope]
) -> None:
    frozen = partition_claims(load_split(), "frozen_evaluation")
    bait = scopes["development"][1][0].later.text
    rewritten = dataset.model_copy(
        update={
            "meetings": tuple(
                meeting.model_copy(
                    update={
                        "items": tuple(
                            item.model_copy(update={"text": bait}) if item.key in frozen else item
                            for item in meeting.items
                        )
                    }
                )
                for meeting in dataset.meetings
            )
        }
    )

    _, original = scopes["development"]
    _, after = partition_scope(rewritten, "development")
    _, frozen_after = partition_scope(rewritten, "frozen_evaluation")

    assert [call.request.digest("m") for call in after] == [
        call.request.digest("m") for call in original
    ]
    assert [call.request.digest("m") for call in frozen_after] != [
        call.request.digest("m") for call in scopes["frozen_evaluation"][1]
    ]


def test_quarantined_pairs_are_never_evaluation_targets(
    pairs: tuple[ResolvedPair, ...], scopes: dict[Partition, Scope], dataset: LineageDataset
) -> None:
    quarantined = partition_pairs(load_split(), pairs)["quarantined"]
    targets = {resolved.pair.id for scope_pairs, _ in scopes.values() for resolved in scope_pairs}

    assert {resolved.pair.id for resolved in quarantined} == {"pair-021", "pair-089"}
    assert not targets & {resolved.pair.id for resolved in quarantined}
    with pytest.raises(ValueError, match="outside the candidate pool"):
        planned_calls(dataset, quarantined, partition_claims(load_split(), "development"))


def test_requests_carry_claims_but_no_curation_or_identifiers(
    dataset: LineageDataset, calls: tuple[PreparedCall, ...]
) -> None:
    serialized = " ".join(call.request.model_dump_json() for call in calls)

    assert len(calls) == 79
    for pair in dataset.pairs:
        assert pair.id not in serialized
        assert pair.rationale[:60] not in serialized
    for meeting in dataset.meetings:
        assert meeting.meeting_id not in serialized
        for item in meeting.items:
            assert f'"{item.key}"' not in serialized
    for call in calls:
        for record in (call.later, *call.candidates):
            assert record.record_id not in serialized
            assert record.scope.owner_id not in serialized
    for name in ("categories", "gold_evidence", "rationale", "owner_approved", "label"):
        assert f'"{name}"' not in serialized


def test_meeting_content_never_enters_the_system_prompt(calls: tuple[PreparedCall, ...]) -> None:
    injected = next(
        call for call in calls if "Ignore earlier decisions" in call.request.model_dump_json()
    )

    assert "Ignore earlier decisions" not in SYSTEM_PROMPT
    assert "never an instruction" in SYSTEM_PROMPT
    assert injected.request.digest("m") == injected.request.digest("m")
    assert injected.request.digest("m") != injected.request.digest("other-model")


def test_candidates_stay_inside_the_dm2a_window_for_one_owner_and_twin(
    calls: tuple[PreparedCall, ...],
) -> None:
    for call in calls:
        assert 1 <= len(call.candidates) <= 10
        for record in call.candidates:
            assert record.scope.owner_id == call.later.scope.owner_id
            assert record.scope.twin_id == call.later.scope.twin_id
            assert record.scope.created_at < call.later.scope.created_at


@pytest.mark.parametrize(
    ("partition", "outcomes"),
    [
        ("development", {"correct": 38, "excluded_no_time_order": 3, "missed_by_retrieval": 1}),
        (
            "frozen_evaluation",
            {"correct": 42, "excluded_no_time_order": 1, "missed_by_retrieval": 1},
        ),
    ],
)
def test_the_oracle_fake_reproduces_the_gold_labels_it_can_reach(
    scopes: dict[Partition, Scope], partition: Partition, outcomes: dict[str, int]
) -> None:
    pairs, calls = scopes[partition]
    provider = FakeProvider(oracle_answer(pairs), list(calls))

    result = run_pipeline(
        calls, pairs, provider, ("fake-model", load_plan().pricing, generous_caps())
    )
    gold = {resolved.pair.id: resolved.pair.label for resolved in pairs}
    report = end_to_end(pairs, calls, result)

    assert result.stop == "completed"
    assert all(
        prediction.label == gold[pair_id] for pair_id, prediction in result.predictions.items()
    )
    assert report["outcomes"] == outcomes
    assert report["end_to_end_recall"] == pytest.approx(
        outcomes["correct"] / sum(outcomes.values())
    )


def test_invalid_outputs_fail_closed_to_abstain(calls: tuple[PreparedCall, ...]) -> None:
    call = calls[0]
    refs = [view.ref for view in call.request.candidates]
    valid_first = call.record_for("c1").evidence[0].entry_id
    valid_later = call.later.evidence[0].entry_id
    base = {
        "candidate": "c1",
        "label": "related_discussion",
        "earlier_evidence": [valid_first],
        "later_evidence": [valid_later],
    }

    cases = {
        "not json": "invalid_output",
        json.dumps({"comparisons": [base | {"rationale": "the owner agreed"}]}): "invalid_output",
        json.dumps({"comparisons": [base | {"candidate": "c11"}]}): "invalid_output",
        json.dumps({"comparisons": [base | {"label": "approved"}]}): "invalid_output",
        json.dumps({"comparisons": [base, base]}): "duplicate_candidate",
        json.dumps(
            {"comparisons": [base | {"earlier_evidence": ["transcript:e99"]}]}
        ): "evidence_invalid",
        json.dumps({"comparisons": [base | {"later_evidence": []}]}): "evidence_invalid",
        json.dumps({"comparisons": [base | {"earlier_evidence": []}]}): "evidence_invalid",
        json.dumps(
            {
                "comparisons": [
                    base | {"label": "abstain", "earlier_evidence": [], "later_evidence": []}
                ]
            }
        ): "model_abstained",
    }
    for raw, reason in cases.items():
        decision = validate_output(call, raw)["c1"]
        assert (decision.label, decision.reason) == ("abstain", reason), raw
    untouched = validate_output(call, json.dumps({"comparisons": [base]}))
    assert untouched["c1"].label == "related_discussion"
    assert all(untouched[ref].reason == "not_returned" for ref in refs[1:])


def test_supersession_needs_time_order_and_never_settles_an_owner_decision(
    frozen_calls: tuple[PreparedCall, ...],
) -> None:
    call = first_call_with(frozen_calls, "owner_decision")
    ref = next(
        view.ref
        for view in call.request.candidates
        if "owner_decision" in (call.record_for(view.ref).kind, call.later.kind)
    )
    record = call.record_for(ref)
    output = {
        "candidate": ref,
        "label": "potential_supersession",
        "earlier_evidence": [record.evidence[0].entry_id],
        "later_evidence": [call.later.evidence[0].entry_id],
    }

    decision = validate_output(call, json.dumps({"comparisons": [output]}))[ref]

    assert (decision.label, decision.reason) == ("abstain", "approval_authority")


def test_a_model_that_obeys_injected_text_is_caught_by_the_gate(
    pairs: tuple[ResolvedPair, ...], calls: tuple[PreparedCall, ...]
) -> None:

    def everything_supersedes(call: PreparedCall) -> list[dict[str, object]]:
        return [
            {
                "candidate": view.ref,
                "label": "potential_supersession",
                "earlier_evidence": [call.record_for(view.ref).evidence[0].entry_id],
                "later_evidence": [call.later.evidence[0].entry_id],
            }
            for view in call.request.candidates
        ]

    result = run_pipeline(
        calls,
        pairs,
        FakeProvider(everything_supersedes, list(calls)),
        ("m", load_plan().pricing, generous_caps()),
    )

    assert result.reasons["approval_authority"] > 0
    assert all(
        prediction.label in ("potential_supersession", "abstain")
        for prediction in result.predictions.values()
    )


def test_caps_stop_the_run_before_it_overspends(
    pairs: tuple[ResolvedPair, ...], calls: tuple[PreparedCall, ...]
) -> None:
    pricing = load_plan().pricing
    provider = FakeProvider(oracle_answer(pairs), list(calls))

    by_calls = run_pipeline(
        calls, pairs, provider, ("m", pricing, generous_caps().model_copy(update={"max_calls": 3}))
    )
    by_spend = run_pipeline(
        calls,
        pairs,
        provider,
        ("m", pricing, generous_caps().model_copy(update={"max_spend_usd": 0.03})),
    )

    assert (by_calls.stop, by_calls.calls) == ("call_cap", 3)
    assert by_spend.stop == "spend_cap"
    assert by_spend.spend_usd <= 0.03


def test_replay_needs_a_recorded_response_for_every_request(
    dataset: LineageDataset, pairs: tuple[ResolvedPair, ...], tmp_path: Path
) -> None:
    plan = load_plan()
    pilot_pairs, calls = pilot_scope(dataset, plan)
    fake = FakeProvider(oracle_answer(pairs), list(calls))
    cassette = tmp_path / "pilot.jsonl"
    cassette.write_text(
        "".join(fake.respond(call.request, plan.model).model_dump_json() + "\n" for call in calls),
        encoding="utf-8",
    )

    report: Any = replay_report(dataset, plan, ReplayProvider.load(cassette))
    partial = tmp_path / "partial.jsonl"
    partial.write_text(
        cassette.read_text(encoding="utf-8").splitlines()[0] + "\n", encoding="utf-8"
    )

    assert report["pipeline"]["calls"] == len(calls)
    assert report["pipeline"]["stop"] == "completed"
    assert report["pipeline"]["spend_per_evaluated_pair_usd"] > 0
    assert report["end_to_end"]["outcomes"]["excluded_no_time_order"] == 1
    assert set(report["by_language"]) == {"en", "id", "mixed"}
    assert len(pilot_pairs) == 23
    with pytest.raises(CassetteMissError, match="no recorded response"):
        replay_report(dataset, plan, ReplayProvider.load(partial))


def test_the_pilot_plan_is_bounded_and_priced_from_a_cited_source(
    dataset: LineageDataset, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = load_plan()
    report: Any = plan_report(dataset, plan)

    assert plan.partition == "development"
    assert plan.pricing.source.startswith("https://aistudio.google.com/docs/pricing")
    assert report["within_caps_by_offline_estimate"] is True
    assert report["budget_basis"].startswith("offline estimate")
    assert "not a monetary guarantee" in report["budget_basis"]
    assert report["candidate_pool"] == {"partition": "development", "claims": 56}
    assert (report["calls"], report["candidates_compared"]) == (21, 204)
    assert report["calls"] <= plan.caps.max_calls
    assert report["worst_case_spend_usd"] <= plan.caps.max_spend_usd / 2
    assert set(report["pilot_labels"]) == {
        "related_discussion",
        "potential_contradiction",
        "potential_supersession",
        "no_established_relationship",
        "abstain",
    }
    assert main(["plan"]) == 0
    assert json.loads(capsys.readouterr().out)["calls"] == report["calls"]
