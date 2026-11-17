import json
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from google.genai import errors as genai_errors
from google.genai import types
from langchain_core.messages import AIMessage, BaseMessage

import evals._lineage_live as live
from evals._decision_lineage import (
    FIXTURE,
    SPLIT_FIXTURE,
    LineageDataset,
    Prediction,
    PredictionSet,
    ResolvedPair,
    gate_failures,
    load_dataset,
    resolve_pairs,
    score,
)
from evals._lineage_classifier import (
    PILOT_FIXTURE,
    ClassifierOutput,
    PilotPlan,
    PreparedCall,
    ReplayProvider,
    RunCaps,
    load_plan,
    partition_scope,
    pilot_scope,
    replay_report,
    run_pipeline,
    spend,
    validate_output,
)
from evals._lineage_live import (
    GEMINI_SCHEMA_KEYWORDS,
    ApprovalSpentError,
    ExecutionApproval,
    GeminiPilotProvider,
    RunInputs,
    approval_problems,
    claim_approval,
    create_model,
    file_digest,
    input_token_bound,
    preflight,
    response_schema,
)

SECRET = "AIzaPlaceholderNeverARealKey0000000000"
Script = list[AIMessage | BaseException | str]


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("a provider call was attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(scope="module")
def dataset() -> LineageDataset:
    return load_dataset()


@pytest.fixture(scope="module")
def plan() -> PilotPlan:
    return load_plan()


@pytest.fixture(scope="module")
def pilot(
    dataset: LineageDataset, plan: PilotPlan
) -> tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]]:
    return pilot_scope(dataset, plan)


def oracle_text(call: PreparedCall, pairs: tuple[ResolvedPair, ...]) -> str:
    gold = {
        (resolved.first.record.record_id, resolved.second.record.record_id): resolved
        for resolved in pairs
        if resolved.first.record is not None and resolved.second.record is not None
    }
    comparisons = []
    for view in call.request.candidates:
        earlier = call.record_for(view.ref)
        resolved = gold.get((earlier.record_id, call.later.record_id))
        label = resolved.pair.label if resolved else "no_established_relationship"
        evidence = resolved.pair.gold_evidence if resolved else None
        comparisons.append(
            {
                "candidate": view.ref,
                "label": label,
                "earlier_evidence": []
                if label == "abstain"
                else list(evidence.first)
                if evidence
                else [earlier.evidence[0].entry_id],
                "later_evidence": []
                if label == "abstain"
                else list(evidence.second)
                if evidence
                else [call.later.evidence[0].entry_id],
            }
        )
    return json.dumps({"comparisons": comparisons})


def answered(text: str, output_tokens: int = 400, thinking: int = 150) -> AIMessage:
    return AIMessage(
        content=text,
        usage_metadata={
            "input_tokens": 1500,
            "output_tokens": output_tokens,
            "total_tokens": 1500 + output_tokens,
            "output_token_details": {"reasoning": thinking},
        },
    )


def server_error(status: int = 503, message: str = "backend unavailable") -> Exception:
    return genai_errors.APIError(status, {"error": {"code": status, "message": message}})


@dataclass
class ScriptedModel:
    script: Script | None = None
    answer: Any = None
    invocations: list[dict[str, object]] = field(default_factory=list)

    def invoke(self, input: list[BaseMessage], **kwargs: Any) -> AIMessage:
        self.invocations.append(kwargs)
        step = self.script.pop(0) if self.script else self.answer(input)
        if isinstance(step, BaseException):
            raise step
        return step if isinstance(step, AIMessage) else answered(step)


@dataclass
class Sink:
    lines: list[str] = field(default_factory=list)

    def __call__(self, line: str) -> None:
        self.lines.append(line)


HEAD = "1" * 40


def approved_inputs() -> RunInputs:
    return RunInputs(
        head=HEAD,
        plan_sha256=file_digest(PILOT_FIXTURE),
        dataset_sha256=file_digest(FIXTURE),
        split_sha256=file_digest(SPLIT_FIXTURE),
        modified=(),
    )


def provenance(plan: PilotPlan) -> Any:
    return live.provenance_for(plan, approved_inputs(), "dm2b-pilot-test")


def provider_for(
    plan: PilotPlan, model: ScriptedModel, sink: Sink | None = None
) -> GeminiPilotProvider:
    return GeminiPilotProvider(model, plan, provenance(plan), sink or Sink(), SECRET)


def oracle_model(
    pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]],
    pairs: tuple[ResolvedPair, ...],
) -> ScriptedModel:
    by_text = {call.request.model_dump_json(): call for call in pilot[1]}

    def answer(input: list[BaseMessage]) -> AIMessage:
        return answered(oracle_text(by_text[str(input[1].content)], pairs))

    return ScriptedModel(answer=answer)


def capped(plan: PilotPlan, **caps: Any) -> PilotPlan:
    return plan.model_copy(update={"caps": RunCaps(**(plan.caps.model_dump() | caps))})


def test_a_request_through_the_real_langchain_stack_carries_every_pilot_setting(
    plan: PilotPlan,
    pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = pilot[1][0]
    model: Any = create_model(plan, SECRET)
    sent: list[dict[str, Any]] = []
    text = oracle_text(call, pilot[0])

    def generate_content(**request: Any) -> types.GenerateContentResponse:
        sent.append(request)
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(role="model", parts=[types.Part(text=text)]),
                    finish_reason=types.FinishReason.STOP,
                )
            ],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=1500,
                candidates_token_count=250,
                thoughts_token_count=150,
                total_token_count=1900,
            ),
        )

    monkeypatch.setattr(model.client.models, "generate_content", generate_content)
    sink = Sink()
    provider = GeminiPilotProvider(model, plan, provenance(plan), sink, SECRET)

    entry = provider.respond(call.request, plan.model)
    config = sent[0]["config"]

    assert len(sent) == 1
    assert sent[0]["model"] == "gemini-3.8-flash"
    assert config.max_output_tokens == plan.caps.max_output_tokens_per_call
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert config.service_tier == types.ServiceTier.STANDARD
    assert config.response_mime_type == "application/json"
    assert config.response_json_schema == response_schema(call.request)
    assert config.http_options.timeout == 60_000
    assert config.http_options.retry_options.attempts == 1
    assert config.tools is None
    assert (entry.input_tokens, entry.output_tokens, entry.thinking_tokens) == (1500, 400, 150)
    assert entry.response == text
    assert validate_output(call, entry.response)
    assert SECRET not in "".join(sink.lines)


def test_the_response_schema_uses_only_supported_keywords_and_the_presented_refs(
    pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]],
) -> None:
    def keywords(node: object) -> Iterator[str]:
        if isinstance(node, dict):
            for key, value in node.items():
                yield key
                children = value.values() if key == "properties" else [value]
                for child in children:
                    yield from keywords(child)

    for call in pilot[1]:
        schema: Any = response_schema(call.request)
        item = schema["properties"]["comparisons"]["items"]
        assert set(keywords(schema)) - {"comparisons"} <= GEMINI_SCHEMA_KEYWORDS | set(
            item["properties"]
        )
        assert item["properties"]["candidate"]["enum"] == [
            view.ref for view in call.request.candidates
        ]
        assert set(ClassifierOutput.model_fields) == set(schema["properties"])


def test_the_input_bound_is_never_below_the_offline_estimate(
    plan: PilotPlan, pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]]
) -> None:
    report: Any = preflight(plan, [call.request for call in pilot[1]])

    for call in pilot[1]:
        request = call.request
        assert input_token_bound(request, 0) >= len(request.model_dump_json().encode())
    assert report["within_caps"] is True
    assert report["max_requests_including_retries"] == plan.caps.max_calls
    assert report["worst_case"]["spend_usd"] <= plan.caps.max_spend_usd


def test_a_run_records_actual_usage_including_thinking_and_replays_offline(
    dataset: LineageDataset,
    plan: PilotPlan,
    pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]],
    tmp_path: Path,
) -> None:
    pairs = resolve_pairs(dataset)
    sink = Sink()
    provider = provider_for(plan, oracle_model(pilot, pairs), sink)

    report: Any = replay_report(dataset, plan, provider)
    cassette = tmp_path / "pilot.jsonl"
    cassette.write_text("".join(sink.lines), encoding="utf-8")
    replayed: Any = replay_report(dataset, plan, ReplayProvider.load(cassette))
    first = json.loads(sink.lines[0])

    assert report["pipeline"]["stop"] == "completed"
    assert provider.ledger.attempts == len(pilot[1])
    assert provider.ledger.thinking_tokens == 150 * len(pilot[1])
    assert provider.ledger.spend(plan) == pytest.approx(
        (1500 * 0.75 + 400 * 3.75) * len(pilot[1]) / 1_000_000
    )
    assert first["thinking_tokens"] == 150
    assert first["provenance"]["service_tier"] == "standard"
    assert first["provenance"]["thinking_level"] == "low"
    assert SECRET not in "".join(sink.lines)
    assert replayed["pipeline"]["decisions"] == report["pipeline"]["decisions"]
    assert replayed["gate_failures"] == report["gate_failures"]


def test_a_transport_error_is_retried_once_and_both_attempts_count(
    plan: PilotPlan, pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]]
) -> None:
    call = pilot[1][0]
    sink = Sink()
    model = ScriptedModel(script=[httpx.ConnectTimeout("slow"), oracle_text(call, pilot[0])])
    provider = provider_for(plan, model, sink)

    entry = provider.respond(call.request, plan.model)

    assert (provider.ledger.attempts, provider.ledger.failed_attempts) == (2, 1)
    assert entry.attempt == 2
    assert [json.loads(line).get("outcome") for line in sink.lines] == ["error", "response"]
    assert json.loads(sink.lines[0])["error_kind"] == "timeout"
    assert provider.ledger.reserved_usd > 0


def test_three_consecutive_provider_errors_stop_the_run(
    plan: PilotPlan, pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]]
) -> None:
    pairs, calls = pilot
    provider = provider_for(plan, ScriptedModel(script=[server_error() for _ in range(10)]))

    result = run_pipeline(calls, pairs, provider, (plan.model, plan.pricing, plan.caps))

    assert result.stop == "provider_errors"
    assert provider.ledger.attempts == 3
    assert result.failed_calls == 1
    assert result.calls == 0


def test_a_rejected_request_stops_at_once_without_retry(
    plan: PilotPlan, pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]]
) -> None:
    pairs, calls = pilot
    model = ScriptedModel(script=[server_error(400, "invalid schema")])
    provider = provider_for(plan, model)

    result = run_pipeline(calls, pairs, provider, (plan.model, plan.pricing, plan.caps))

    assert (result.stop, provider.ledger.attempts) == ("provider_rejected", 1)


def test_invalid_structured_output_is_never_retried_and_its_rate_stops_the_run(
    plan: PilotPlan, pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]]
) -> None:
    pairs, calls = pilot
    provider = provider_for(plan, ScriptedModel(answer=lambda _: answered("not json")))

    result = run_pipeline(
        calls, pairs, provider, (plan.model, plan.pricing, plan.caps), plan.stop_rules
    )

    assert result.stop == "invalid_output_rate"
    assert provider.ledger.attempts == result.calls == plan.stop_rules.invalid_output_after_calls
    assert result.invalid_calls == result.calls


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": 1500},
        {"input_tokens": 1500, "output_tokens": None},
        {"input_tokens": 0, "output_tokens": 400},
        {"input_tokens": -1, "output_tokens": 400},
        {"input_tokens": "1500", "output_tokens": 400},
        {"input_tokens": 1500.5, "output_tokens": 400},
        {"input_tokens": True, "output_tokens": 400},
        {"input_tokens": 1500, "output_tokens": 400, "output_token_details": {"reasoning": 401}},
        {"input_tokens": 1500, "output_tokens": 400, "output_token_details": {"reasoning": -1}},
        {"input_tokens": 1500, "output_tokens": 400, "output_token_details": "thinking"},
        "1500 in, 400 out",
    ],
)
def test_absent_incomplete_or_invalid_usage_is_unknown_and_never_retried(
    plan: PilotPlan,
    pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]],
    usage: object,
) -> None:
    pairs, calls = pilot
    sink = Sink()
    reply = AIMessage.model_construct(
        content='{"comparisons": []}', usage_metadata=usage, tool_calls=[]
    )
    model = ScriptedModel(answer=lambda _: reply)
    provider = provider_for(plan, model, sink)

    result = run_pipeline(calls, pairs, provider, (plan.model, plan.pricing, plan.caps))
    worst = spend(
        plan.pricing,
        input_token_bound(calls[0].request, plan.live.message_overhead_tokens),
        plan.caps.max_output_tokens_per_call,
    )

    assert result.stop == "usage_unknown"
    assert len(model.invocations) == provider.ledger.attempts == 1
    assert (provider.ledger.input_tokens, provider.ledger.output_tokens) == (0, 0)
    assert provider.ledger.reserved_usd == pytest.approx(worst)
    assert provider.ledger.spend(plan) == pytest.approx(worst)
    assert [json.loads(line)["outcome"] for line in sink.lines] == ["usage_unknown"]
    assert SECRET not in sink.lines[0]
    assert set(json.loads(sink.lines[0])) >= {"request_digest", "attempt", "provenance"}


def test_usage_beyond_the_bound_stops_the_run_as_unexpected_cost(
    plan: PilotPlan, pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]]
) -> None:
    pairs, calls = pilot
    over = plan.caps.max_output_tokens_per_call + 1
    provider = provider_for(
        plan, ScriptedModel(answer=lambda _: answered('{"comparisons": []}', over, over - 10))
    )

    result = run_pipeline(calls, pairs, provider, (plan.model, plan.pricing, plan.caps))

    assert result.stop == "usage_over_bound"
    assert provider.ledger.output_tokens == over


def test_retries_and_spend_count_against_the_caps(
    plan: PilotPlan, pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]]
) -> None:
    pairs, calls = pilot
    flaky = ScriptedModel(
        script=[httpx.ConnectError("reset"), oracle_text(calls[0], pairs)],
        answer=lambda _: answered('{"comparisons": []}'),
    )
    by_requests = provider_for(capped(plan, max_calls=3), flaky)
    by_spend = provider_for(
        capped(plan, max_spend_usd=0.04),
        ScriptedModel(answer=lambda _: answered('{"comparisons": []}')),
    )

    requests = run_pipeline(
        calls, pairs, by_requests, (plan.model, plan.pricing, by_requests.plan.caps)
    )
    spent = run_pipeline(calls, pairs, by_spend, (plan.model, plan.pricing, by_spend.plan.caps))

    assert (requests.stop, by_requests.ledger.attempts, requests.calls) == ("call_cap", 3, 2)
    assert spent.stop == "spend_cap"
    assert by_spend.ledger.spend(plan) <= 0.04


def test_error_details_and_secrets_never_reach_the_cassette(
    plan: PilotPlan, pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]]
) -> None:
    pairs, calls = pilot
    sink = Sink()
    echoing = ScriptedModel(
        script=[server_error(503, f"bad key {SECRET} at /v1beta/models"), f"echo {SECRET}"]
    )
    provider = provider_for(plan, echoing, sink)

    result = run_pipeline(calls, pairs, provider, (plan.model, plan.pricing, plan.caps))
    recorded = [json.loads(line) for line in sink.lines]

    assert result.stop == "secret_in_record"
    assert [line["outcome"] for line in recorded] == ["error"]
    assert (recorded[0]["error_kind"], recorded[0]["error_status"]) == ("unavailable", 503)
    assert "detail" not in " ".join(recorded[0])
    assert SECRET not in "".join(sink.lines)
    assert "v1beta" not in "".join(sink.lines)


def test_participant_approval_assertions_never_become_owner_authority(
    dataset: LineageDataset,
) -> None:
    frozen_pairs, frozen_calls = partition_scope(dataset, "frozen_evaluation")
    claims = {
        resolved.pair.id: resolved
        for resolved in frozen_pairs
        if "owner_approval_claim" in resolved.pair.categories
    }
    predictions = []
    for call in frozen_calls:
        for view in call.request.candidates:
            earlier = call.record_for(view.ref)
            match = next(
                (
                    resolved
                    for resolved in claims.values()
                    if resolved.first.record == earlier and resolved.second.record == call.later
                ),
                None,
            )
            if match is None:
                continue
            raw = json.dumps(
                {
                    "comparisons": [
                        {
                            "candidate": view.ref,
                            "label": "potential_supersession",
                            "earlier_evidence": [earlier.evidence[0].entry_id],
                            "later_evidence": [call.later.evidence[0].entry_id],
                        }
                    ]
                }
            )
            decision = validate_output(call, raw)[view.ref]
            predictions.append((match, decision))

    blocked = {resolved.pair.id for resolved, decision in predictions if decision.reason}
    passed = {resolved.pair.id for resolved, decision in predictions if not decision.reason}
    result = score(
        PredictionSet(
            predictor="approval-follower",
            predictions=tuple(
                Prediction(
                    pair_id=resolved.pair.id, label=decision.label, evidence=decision.evidence
                )
                for resolved, decision in predictions
            ),
        ),
        tuple(claims.values()),
    )

    assert blocked == {
        pair_id
        for pair_id, resolved in claims.items()
        if "owner_decision" in (resolved.first.item.kind, resolved.second.item.kind)
    }
    assert passed
    assert result.approval_resolutions == len(passed)
    assert "approval_resolutions" in " ".join(gate_failures(result))


def approval_for(plan: PilotPlan, **changes: Any) -> ExecutionApproval:
    inputs = approved_inputs()
    return ExecutionApproval.model_validate(
        {
            "approval_id": "dm2b-pilot-test",
            "approved_by": "co-founder",
            "approved_on": "2026-11-17",
            "head": inputs.head,
            "plan_sha256": inputs.plan_sha256,
            "dataset_sha256": inputs.dataset_sha256,
            "split_sha256": inputs.split_sha256,
            "provider": plan.provider,
            "model": plan.model,
            "max_calls": plan.caps.max_calls,
            "max_input_tokens": plan.caps.max_input_tokens,
            "max_output_tokens": plan.caps.max_output_tokens,
            "max_output_tokens_per_call": plan.caps.max_output_tokens_per_call,
            "max_spend_usd": plan.caps.max_spend_usd,
        }
        | changes
    )


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"head": "2" * 40}, "head"),
        ({"plan_sha256": "f" * 64}, "plan_sha256"),
        ({"dataset_sha256": "f" * 64}, "dataset_sha256"),
        ({"split_sha256": "f" * 64}, "split_sha256"),
        ({"provider": "openai"}, "provider"),
        ({"model": "gemini-3.8-pro"}, "model"),
        ({"max_calls": 25}, "max_calls"),
        ({"max_input_tokens": 200_000}, "max_input_tokens"),
        ({"max_output_tokens": 200_000}, "max_output_tokens"),
        ({"max_output_tokens_per_call": 8192}, "max_output_tokens_per_call"),
        ({"max_spend_usd": 5.0}, "max_spend_usd"),
    ],
)
def test_an_approval_binds_every_input_and_cap(
    plan: PilotPlan, change: dict[str, object], problem: str
) -> None:
    assert approval_problems(approval_for(plan), plan, approved_inputs()) == []
    problems = approval_problems(approval_for(plan, **change), plan, approved_inputs())
    assert [item.split(":")[0] for item in problems] == [problem]


def test_locally_modified_tracked_files_refuse_the_run(plan: PilotPlan) -> None:
    dirty = RunInputs(**(vars_of(approved_inputs()) | {"modified": (" M evals/_lineage_live.py",)}))

    assert approval_problems(approval_for(plan), plan, dirty) == [
        "tracked files modified locally: [' M evals/_lineage_live.py']"
    ]


def vars_of(inputs: RunInputs) -> dict[str, Any]:
    return {name: getattr(inputs, name) for name in RunInputs.__slots__}


@dataclass
class Harness:
    tmp_path: Path
    plan: PilotPlan
    credentials: list[str] = field(default_factory=list)
    model: Any = None

    def approval(self, **changes: Any) -> Path:
        path = self.tmp_path / f"approval-{len(list(self.tmp_path.glob('approval-*')))}.json"
        path.write_text(approval_for(self.plan, **changes).model_dump_json(), encoding="utf-8")
        return path

    def run(self, approval: Path, cassette: str, *extra: str) -> int:
        return live.main(
            [
                *extra,
                "run",
                "--approval",
                str(approval),
                "--cassette",
                str(self.tmp_path / cassette),
            ]
        )

    def receipts(self) -> list[Path]:
        return sorted((self.tmp_path / "receipts").glob("*.json"))


@pytest.fixture
def harness(
    plan: PilotPlan,
    dataset: LineageDataset,
    pilot: tuple[tuple[ResolvedPair, ...], tuple[PreparedCall, ...]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Harness:
    state = Harness(tmp_path, plan, model=oracle_model(pilot, resolve_pairs(dataset)))

    def key(_: PilotPlan) -> str:
        state.credentials.append("read")
        return SECRET

    monkeypatch.setattr(live, "RECEIPTS", tmp_path / "receipts")
    monkeypatch.setattr(live, "git_head", lambda: HEAD)
    monkeypatch.setattr(live, "tracked_modifications", tuple)
    monkeypatch.setattr(live, "debrief_key", key)
    monkeypatch.setattr(live, "create_model", lambda *_: state.model)
    return state


def test_an_approval_runs_once_and_never_again_with_another_cassette(
    harness: Harness, capsys: pytest.CaptureFixture[str]
) -> None:
    approval = harness.approval()

    assert harness.run(approval, "first.jsonl") == 0
    first: Any = json.loads(capsys.readouterr().out)
    assert harness.run(approval, "second.jsonl") == 2
    refusal = json.loads(capsys.readouterr().out)
    receipt = harness.receipts()[0].read_text(encoding="utf-8").splitlines()

    assert first["pipeline"]["stop"] == "completed"
    assert first["ledger"]["attempts"] == 21
    assert "already claimed" in refusal["refused"]
    assert harness.credentials == ["read"]
    assert not (harness.tmp_path / "second.jsonl").exists()
    assert [json.loads(line).get("outcome") for line in receipt] == [None, "completed"]
    assert json.loads(receipt[0])["cassette"].endswith("first.jsonl")


def test_parallel_claims_let_exactly_one_thread_through(plan: PilotPlan, tmp_path: Path) -> None:
    approval = approval_for(plan)
    start = threading.Barrier(8)
    outcomes: list[str] = []

    def contend(index: int) -> None:
        start.wait()
        try:
            claim_approval(approval, tmp_path, tmp_path / f"{index}.jsonl")
            outcomes.append("claimed")
        except ApprovalSpentError:
            outcomes.append("refused")

    threads = [threading.Thread(target=contend, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["claimed"] + ["refused"] * 7
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_parallel_processes_cannot_both_claim(plan: PilotPlan, tmp_path: Path) -> None:
    approval = tmp_path / "approval.json"
    approval.write_text(approval_for(plan).model_dump_json(), encoding="utf-8")
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from evals._lineage_live import ApprovalSpentError, ExecutionApproval, claim_approval\n"
        "approval = ExecutionApproval.model_validate_json(Path(sys.argv[1]).read_text())\n"
        "try:\n"
        "    claim_approval(approval, Path(sys.argv[2]), Path(sys.argv[3]))\n"
        "except ApprovalSpentError:\n"
        "    raise SystemExit(3)\n"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(approval), str(tmp_path / "receipts"), f"{index}"]
        )
        for index in range(4)
    ]

    codes = sorted(process.wait(timeout=60) for process in processes)

    assert codes == [0, 3, 3, 3]


@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), RuntimeError("worker crashed")])
def test_an_interrupted_or_failed_run_spends_the_approval(
    harness: Harness, interruption: BaseException
) -> None:
    harness.model = ScriptedModel(script=[interruption])
    approval = harness.approval()

    if isinstance(interruption, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            harness.run(approval, "first.jsonl")
    else:
        assert harness.run(approval, "first.jsonl") == 0
    receipt = harness.receipts()[0].read_text(encoding="utf-8").splitlines()
    assert harness.run(approval, "retry.jsonl") == 2

    assert harness.credentials == ["read"]
    assert json.loads(receipt[-1])["outcome"] == (
        "aborted: KeyboardInterrupt"
        if isinstance(interruption, KeyboardInterrupt)
        else "provider_rejected"
    )
    assert not (harness.tmp_path / "retry.jsonl").exists()


def test_a_replaced_dataset_is_refused_even_with_the_same_head_and_plan(
    harness: Harness, dataset: LineageDataset, capsys: pytest.CaptureFixture[str]
) -> None:
    swapped = harness.tmp_path / "dataset.json"
    meetings = list(dataset.meetings)
    first = meetings[0]
    edited = first.items[0].model_copy(update={"text": "Real meeting text"})
    meetings[0] = first.model_copy(update={"items": (edited, *first.items[1:])})
    swapped.write_text(
        dataset.model_copy(update={"meetings": tuple(meetings)}).model_dump_json(),
        encoding="utf-8",
    )

    assert harness.run(harness.approval(), "pilot.jsonl", "--dataset", str(swapped)) == 2
    refusal = json.loads(capsys.readouterr().out)

    assert [item.split(":")[0] for item in refusal["problems"]] == ["dataset_sha256"]
    assert harness.credentials == []
    assert harness.receipts() == []


def test_a_mismatched_or_dirty_run_never_claims_or_reads_credentials(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    approval = harness.approval()
    monkeypatch.setattr(live, "git_head", lambda: "2" * 40)
    assert harness.run(approval, "pilot.jsonl") == 2
    monkeypatch.setattr(live, "git_head", lambda: HEAD)
    monkeypatch.setattr(live, "tracked_modifications", lambda: (" M evals/_lineage_live.py",))
    assert harness.run(approval, "pilot.jsonl") == 2
    output = capsys.readouterr().out

    assert "head" in output
    assert "tracked files modified locally" in output
    assert harness.credentials == []
    assert harness.receipts() == []
    with pytest.raises(SystemExit):
        live.main(["run", "--cassette", str(harness.tmp_path / "pilot.jsonl")])
