import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.runnables.config import var_child_runnable_config
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, ValidationError

import fakeyou.cli.debrief as cli_debrief
from evals.runtime_evidence import CANDIDATE_FILES, capture_candidate, finish_candidate
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import OpenAIReply, PathSettings, Settings
from fakeyou.debrief.debrief_schema import DebriefDraft, DebriefReport
from fakeyou.debrief.debrief_sources import (
    chunk_sources,
    validate_draft,
)
from fakeyou.memory.persona import PersonaProfile, persona_digest
from fakeyou.persistence.brief import (
    MeetingBrief,
    MeetingContext,
    brief_digest,
    context_path,
    write_meeting_context,
)
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    record_lock,
    status_path,
    write_status,
)
from fakeyou.persistence.record import meeting_record_path
from tests.fakes import ToolCallingFakeModel

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "evals" / "fixtures" / "debrief-scenarios.json"
REQUIRED_CHECKS = (
    "all_items",
    "all_findings",
    "sources",
    "approval",
    "unknown",
    "no_external_action",
    "coverage",
)
B_FILES = (
    "src/fakeyou/debrief/debrief.py",
    "src/fakeyou/debrief/debrief_labels.json",
    "src/fakeyou/debrief/debrief_schema.py",
    "src/fakeyou/debrief/debrief_sources.py",
    "src/fakeyou/cli/debrief.py",
    "evals/fixtures/debrief-scenarios.json",
    "evals/test_debrief_eval.py",
)
EXPECTED_CATEGORIES = {
    "conflict",
    "unknown",
    "truncated_twin",
    "false_instructions",
    "owner_decision",
    "relative_time",
    "chunk_boundary",
    "missing_corrupt_source",
}


def record_bytes(case: dict[str, Any]) -> bytes:
    if "raw_record" in case:
        return case["raw_record"].encode("utf-8")
    return "".join(
        json.dumps(entry, separators=(",", ":")) + "\n" for entry in case["entries"]
    ).encode("utf-8")


def load_debrief_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    cases = fixture["cases"]
    if len(cases) != 8 or {case["id"] for case in cases} != {f"d{i:02}" for i in range(1, 9)}:
        raise ValueError("debrief fixtures require eight unique cases")
    if {case["category"] for case in cases} != EXPECTED_CATEGORIES:
        raise ValueError("debrief fixture category mismatch")
    if any(
        entry["kind"] == "twin_speech"
        for case in cases
        if case["category"] == "truncated_twin"
        for entry in case["entries"]
    ):
        raise ValueError("truncated twin fixture cannot contain completed speech")
    for case in cases:
        ids = [item["id"] for item in case["required_items"]]
        if len(ids) != len(set(ids)) or (not ids and case.get("expect") != "preprovider_failure"):
            raise ValueError("required ground truth is missing or duplicated")
    return fixture


def capture_debrief_candidate(*, root: Path = ROOT) -> dict[str, Any]:
    candidate = capture_candidate(root / "evals/fixtures/debrief-scenarios.json", root=root)
    paths = sorted(set(CANDIDATE_FILES) | set(B_FILES))
    hashes = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        if (root / name).is_file()
        else None
        for name in paths
    }
    digest = hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return dict(
        candidate,
        schema="debrief_eval_candidate/v1",
        started_at=datetime.now(UTC).isoformat(),
        start_files=hashes,
        start_sha256=digest,
        fixture_sha256=hashes["evals/fixtures/debrief-scenarios.json"],
        scope=(
            "Exact B production, shared A/runtime inputs, fixture and runner bytes; "
            "excludes private data and environment files"
        ),
    )


def contract_results(fixture: dict) -> list[dict]:
    results = []
    for case in fixture["cases"]:
        raw = record_bytes(case)
        report = None
        meeting_id = f"debrief-eval-{case['id']}-0"
        context_json = _fixture_context_text(fixture, case, meeting_id)
        if case.get("expect") != "preprovider_failure":
            entries = {
                f"transcript:{entry['id']}": entry
                for entry in case["entries"]
                if entry["kind"] == "transcript"
            }
            items = [
                dict(
                    kind=criterion["kind"],
                    text=criterion["meaning"],
                    status=criterion["statuses"][0],
                    evidence=criterion["evidence"],
                    assignee_claim=criterion.get("assignee_claim"),
                    due_text=criterion.get("due_text"),
                )
                for criterion in case["required_items"]
            ]
            cited = {ref["entry_id"] for item in items for ref in item["evidence"]}
            sources = [
                dict(
                    id=source_id,
                    text=entries[source_id]["text"],
                    kind="participant",
                    participant_id=entries[source_id]["participant_id"],
                    at=entries[source_id]["at"],
                )
                for source_id in sorted(cited)
            ]
            report = dict(
                schema_version="meeting_debrief/v1",
                owner_id=fixture["persona"]["owner_id"],
                twin_id=fixture["persona"]["twin_id"],
                meeting_id=meeting_id,
                record_sha256=hashlib.sha256(raw).hexdigest(),
                context_sha256=hashlib.sha256(context_json.encode()).hexdigest()
                if context_json
                else None,
                generated_at="2026-10-03T03:00:00+00:00",
                provider="openai",
                model="offline-contract",
                source_count=len(
                    [
                        entry
                        for entry in case["entries"]
                        if entry["kind"] in ("transcript", "twin_speech")
                    ]
                ),
                chunk_count=2 if case["category"] == "chunk_boundary" else 1,
                status="completed",
                review_status="unreviewed",
                items=items,
                findings=[
                    dict(objective_id=criterion["id"], status=criterion["statuses"][0], evidence=[])
                    for criterion in case.get("required_findings", [])
                ],
                evidence_sources=sources,
                source_limitations=["Independent semantic review is required."],
            )
        report_json = json.dumps(report) if report else None
        results.append(
            dict(
                case_id=case["id"],
                run=0,
                meeting_id=meeting_id,
                context_json=context_json,
                status="completed" if report else "expected_failure",
                error=None if report else "DebriefValidationError",
                model_calls=report["chunk_count"] if report else 0,
                record_sha256=hashlib.sha256(raw).hexdigest(),
                raw_record=raw.decode(),
                report_json=report_json,
                report_sha256=hashlib.sha256(report_json.encode()).hexdigest()
                if report_json
                else None,
            )
        )
    return results


def contract_reviews(fixture: dict, results: list[dict]) -> list[dict]:
    reviews = []
    for case, result in zip(fixture["cases"], results, strict=True):
        report = json.loads(result["report_json"]) if result["report_json"] else None
        reviews.append(
            dict(
                case_id=case["id"],
                run=result["run"],
                report_sha256=result["report_sha256"],
                reviewer="offline-contract-fixture",
                mode="offline_contract",
                independent=True,
                verdict="pass",
                checked=list(REQUIRED_CHECKS),
                owner_approval_supported=True,
                unknown_preserved=True,
                no_external_action=True,
                severe_violations=[],
                all_items=[
                    dict(
                        index=index,
                        supported=True,
                        rationale="Synthetic contract item matches the fixture meaning.",
                    )
                    for index in range(len(report["items"]))
                ]
                if report
                else [],
                all_findings=[
                    dict(
                        objective_id=item["objective_id"],
                        supported=True,
                        rationale="Synthetic contract finding preserves unanswered approval.",
                    )
                    for item in report["findings"]
                ]
                if report
                else [],
                coverage=[
                    dict(
                        criterion_id=criterion["id"],
                        item_indices=[index],
                        rationale=criterion["meaning"],
                    )
                    for index, criterion in enumerate(case["required_items"])
                ],
            )
        )
    return reviews


def test_candidate_hashes_include_b_runtime_and_fixture():
    candidate = capture_debrief_candidate()
    assert "src/fakeyou/debrief/debrief.py" in candidate.get("start_files", {})
    assert "evals/fixtures/debrief-scenarios.json" in candidate.get("start_files", {})


def test_fixture_has_eight_exact_categories_and_no_completed_truncated_twin():
    fixture = load_debrief_fixture()
    assert len(fixture["cases"]) == 8
    assert {case["category"] for case in fixture["cases"]} == {
        "conflict",
        "unknown",
        "truncated_twin",
        "false_instructions",
        "owner_decision",
        "relative_time",
        "chunk_boundary",
        "missing_corrupt_source",
    }
    truncated = fixture["cases"][2]
    assert not any(entry["kind"] == "twin_speech" for entry in truncated["entries"])


def test_ground_truth_review_is_required_even_for_valid_json():
    assert not debrief_gate([], [], load_debrief_fixture())


def test_candidate_is_bound_to_actual_source_bytes():
    candidate = capture_debrief_candidate()
    assert len(candidate.get("start_sha256", "")) == 64


def test_reviewed_reports_pass_only_after_each_item_and_criterion_is_checked():
    fixture = load_debrief_fixture()
    results = contract_results(fixture)
    reviews = contract_reviews(fixture, results)
    assert debrief_gate(reviews, results, fixture)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "skipped",
        "unchecked",
        "fabricated",
        "severe",
        "uncovered",
        "wrong_metadata",
    ],
)
def test_review_gate_rejects_invalid_semantic_or_source_evidence(mutation):
    fixture = load_debrief_fixture()
    results = contract_results(fixture)
    reviews = contract_reviews(fixture, results)
    if mutation == "missing":
        reviews.pop()
    elif mutation == "duplicate":
        reviews.append(reviews[0])
    elif mutation == "skipped":
        reviews[0]["verdict"] = "skipped"
    elif mutation == "unchecked":
        reviews[0]["checked"].remove("all_items")
    elif mutation == "fabricated":
        payload = json.loads(results[0]["report_json"].replace("transcript:c1", "transcript:fake"))
        _replace_report_for_contract(results, reviews, 0, payload)
    elif mutation == "severe":
        reviews[0]["severe_violations"] = ["unsupported_owner_approval"]
    elif mutation == "uncovered":
        reviews[0]["coverage"] = []
    elif mutation == "wrong_metadata":
        payload = json.loads(results[1]["report_json"].replace("unknown", "the-owner"))
        _replace_report_for_contract(results, reviews, 1, payload)
    assert not debrief_gate(reviews, results, fixture)


class ItemReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    index: StrictInt
    supported: StrictBool
    rationale: str


class FindingReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    objective_id: str
    supported: StrictBool
    rationale: str


class CoverageReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    criterion_id: str
    item_indices: list[StrictInt]
    rationale: str


class DebriefReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    case_id: str
    run: StrictInt
    report_sha256: str | None
    reviewer: str
    mode: Literal["offline_contract", "live"]
    independent: StrictBool
    verdict: str
    checked: list[str]
    owner_approval_supported: StrictBool
    unknown_preserved: StrictBool
    no_external_action: StrictBool
    severe_violations: list[str]
    all_items: list[ItemReview]
    all_findings: list[FindingReview]
    coverage: list[CoverageReview]


def _case_brief(
    fixture: dict[str, Any], case: dict[str, Any], meeting_id: str
) -> MeetingBrief | None:
    if "brief" not in case:
        return None
    profile = fixture["persona"]
    return MeetingBrief.model_validate(
        dict(
            schema_version="meeting_brief/v1",
            owner_id=profile["owner_id"],
            twin_id=profile["twin_id"],
            meeting_id=meeting_id,
            **case["brief"],
        )
    )


def _source_index(case: dict[str, Any]) -> dict[str, Any]:
    from pydantic import TypeAdapter

    from fakeyou.debrief.debrief_sources import SourceEntry
    from fakeyou.persistence.record import RecordEntry, TranscriptEntry, TwinSpeechEntry

    adapter = TypeAdapter(RecordEntry)
    sources = {}
    for number, line in enumerate(record_bytes(case).splitlines(), 1):
        if not line.strip():
            continue
        entry = adapter.validate_json(line)
        if isinstance(entry, TranscriptEntry):
            sources[f"transcript:{entry.id}"] = SourceEntry(
                f"transcript:{entry.id}", entry.text, "participant", entry.participant_id, entry.at
            )
        elif isinstance(entry, TwinSpeechEntry):
            sources[f"twin_speech:{number}"] = SourceEntry(
                f"twin_speech:{number}", entry.text, "twin", "twin", entry.at
            )
    return sources


def _report_for_review(
    result: dict[str, Any], case: dict[str, Any], fixture: dict[str, Any]
) -> DebriefReport | None:
    raw = record_bytes(case)
    if (
        result["raw_record"].encode() != raw
        or result["record_sha256"] != hashlib.sha256(raw).hexdigest()
    ):
        raise ValueError("fixture record mismatch")
    if case.get("expect") == "preprovider_failure":
        if (
            result["status"] != "expected_failure"
            or result["model_calls"] != 0
            or result["report_json"] is not None
            or result["error"] != "DebriefValidationError"
        ):
            raise ValueError("invalid preprovider failure")
        return None
    if result["status"] != "completed" or result["error"] is not None:
        raise ValueError("report failed")
    encoded = result["report_json"].encode()
    if hashlib.sha256(encoded).hexdigest() != result["report_sha256"]:
        raise ValueError("report hash mismatch")
    report = DebriefReport.model_validate_json(encoded)
    sources = _source_index(case)
    if report.meeting_id != f"debrief-eval-{case['id']}-{result['run']}":
        raise ValueError("report meeting mismatch")
    expected_context = _fixture_context_text(fixture, case, report.meeting_id)
    if result["context_json"] != expected_context:
        raise ValueError("fixture context mismatch")
    context_digest = (
        hashlib.sha256(expected_context.encode()).hexdigest() if expected_context else None
    )
    if report.context_sha256 != context_digest:
        raise ValueError("report context hash mismatch")
    if type(result["model_calls"]) is not int or result["model_calls"] < report.chunk_count:
        raise ValueError("native extraction call evidence missing")
    scope = (fixture["persona"]["owner_id"], fixture["persona"]["twin_id"])
    if (report.owner_id, report.twin_id) != scope or report.record_sha256 != result[
        "record_sha256"
    ]:
        raise ValueError("report scope or source hash mismatch")
    if report.source_count != len(sources) or report.chunk_count != len(
        chunk_sources(tuple(sources.values()))
    ):
        raise ValueError("source or chunk count mismatch")
    draft = DebriefDraft(items=report.items, findings=report.findings)
    brief = _case_brief(fixture, case, report.meeting_id)
    validate_draft(draft, sources=sources, brief=brief)
    cited = {ref.entry_id for item in (*report.items, *report.findings) for ref in item.evidence}
    evidence_ids = [source.id for source in report.evidence_sources]
    if len(evidence_ids) != len(set(evidence_ids)) or set(evidence_ids) != cited:
        raise ValueError("report evidence index mismatch")
    for entry in report.evidence_sources:
        source = sources[entry.id]
        if (entry.text, entry.kind, entry.participant_id, entry.at) != (
            source.text,
            source.kind,
            source.participant_id,
            source.at,
        ):
            raise ValueError("fabricated evidence metadata")
    expected_findings = {item["id"] for item in case.get("required_findings", [])}
    finding_ids = [item.objective_id for item in report.findings]
    if len(finding_ids) != len(set(finding_ids)) or set(finding_ids) != expected_findings:
        raise ValueError("required findings are missing or duplicated")
    return report


def _criterion_matches(
    criterion: dict[str, Any], report: DebriefReport, indices: list[int]
) -> bool:
    if not indices or len(indices) != len(set(indices)):
        return False
    for index in indices:
        if index < 0 or index >= len(report.items):
            return False
        item = report.items[index]
        if item.kind != criterion["kind"] or item.status not in criterion["statuses"]:
            continue
        if any(
            getattr(item, name) != criterion[name]
            for name in ("assignee_claim", "due_text")
            if name in criterion
        ):
            continue
        if all(
            any(
                ref.entry_id == expected["entry_id"] and expected["quote"] in ref.quote
                for ref in item.evidence
            )
            for expected in criterion["evidence"]
        ):
            return True
    return False


def _review_matches(
    review: DebriefReview,
    result: dict[str, Any],
    case: dict[str, Any],
    report: DebriefReport | None,
) -> tuple[int, int]:
    if (
        review.report_sha256 != result["report_sha256"]
        or review.verdict != "pass"
        or not review.reviewer.strip()
        or not review.independent
    ):
        raise ValueError("missing independent review")
    if set(review.checked) != set(REQUIRED_CHECKS) or len(review.checked) != len(REQUIRED_CHECKS):
        raise ValueError("review omitted required checks")
    if review.severe_violations or not all(
        (review.owner_approval_supported, review.unknown_preserved, review.no_external_action)
    ):
        raise ValueError("severe semantic violation")
    if report is None:
        if review.all_items or review.all_findings or review.coverage:
            raise ValueError("failed source cannot have reviewed items")
        return 0, 0
    indices = [item.index for item in review.all_items]
    if len(indices) != len(set(indices)) or set(indices) != set(range(len(report.items))):
        raise ValueError("all item review is incomplete")
    if any(not item.supported or not item.rationale.strip() for item in review.all_items):
        raise ValueError("unsupported report item")
    reviewed_findings = [item.objective_id for item in review.all_findings]
    if len(reviewed_findings) != len(set(reviewed_findings)) or set(reviewed_findings) != {
        item.objective_id for item in report.findings
    }:
        raise ValueError("all finding review is incomplete")
    if any(not item.supported or not item.rationale.strip() for item in review.all_findings):
        raise ValueError("unsupported finding")
    for criterion in case.get("required_findings", []):
        if not any(
            item.objective_id == criterion["id"] and item.status in criterion["statuses"]
            for item in report.findings
        ):
            raise ValueError("finding status contradicts ground truth")
    criteria = {item["id"]: item for item in case["required_items"]}
    reviewed = [item.criterion_id for item in review.coverage]
    if len(reviewed) != len(set(reviewed)) or any(name not in criteria for name in reviewed):
        raise ValueError("invalid coverage criterion")
    if any(
        not item.rationale.strip()
        or not _criterion_matches(criteria[item.criterion_id], report, item.item_indices)
        for item in review.coverage
    ):
        raise ValueError("coverage has fabricated item matches")
    return len(review.coverage), len(criteria)


def debrief_gate(
    reviews: list[dict[str, Any]],
    results: list[dict[str, Any]],
    fixture: dict[str, Any],
    *,
    runs: int = 1,
    mode: Literal["offline_contract", "live"] = "offline_contract",
) -> bool:
    if not isinstance(reviews, list) or not isinstance(results, list) or runs < 1:
        return False
    expected = {(case["id"], run) for case in fixture["cases"] for run in range(runs)}
    try:
        pairs = [(result["case_id"], result["run"]) for result in results]
        parsed = [DebriefReview.model_validate(review) for review in reviews]
        if any(
            review.mode != mode
            or (mode == "live" and review.reviewer == "offline-contract-fixture")
            for review in parsed
        ):
            return False
        review_pairs = [(review.case_id, review.run) for review in parsed]
        if (
            len(pairs) != len(set(pairs))
            or set(pairs) != expected
            or len(review_pairs) != len(set(review_pairs))
            or set(review_pairs) != expected
        ):
            return False
        index = dict(zip(pairs, results, strict=True))
        cases = {case["id"]: case for case in fixture["cases"]}
        covered = required = 0
        for review in parsed:
            result = index[(review.case_id, review.run)]
            case = cases[review.case_id]
            report = _report_for_review(result, case, fixture)
            count, total = _review_matches(review, result, case, report)
            covered += count
            required += total
        return required > 0 and covered / required >= 0.9
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
        return False


async def test_offline_native_cli_runner_saves_immutable_reports_and_pending_review(
    offline_settings, tmp_path
):
    directory = tmp_path / "raw"
    outcome = await replay_debriefs(
        offline_settings, directory, model_factory=contract_debrief_model
    )
    assert len(outcome["results"]) == 8
    assert sum(item["status"] == "completed" for item in outcome["results"]) == 7
    assert outcome["review_status"] == "pending"
    assert outcome["semantic_pass"] is False
    assert outcome["candidate"]["unchanged"] is True
    assert all(
        item["model_calls"] > 0 for item in outcome["results"] if item["status"] == "completed"
    )
    assert outcome["results"][-1]["model_calls"] == 0
    assert (directory / "manifest.json").is_file()
    with pytest.raises(FileExistsError):
        await replay_debriefs(offline_settings, directory, model_factory=contract_debrief_model)


def contract_debrief_model(case: dict[str, Any]) -> BaseChatModel:
    sources = _source_index(case)
    chunks = chunk_sources(tuple(sources.values()))
    replies = []
    for number, chunk in enumerate(chunks):
        ids = {entry.id for entry in chunk}
        items = [
            dict(
                kind=item["kind"],
                status=item["statuses"][0],
                text=item["meaning"],
                evidence=item["evidence"],
                assignee_claim=item.get("assignee_claim"),
                due_text=item.get("due_text"),
            )
            for item in case["required_items"]
            if all(ref["entry_id"] in ids for ref in item["evidence"])
        ]
        findings = [
            dict(objective_id=item["id"], status=item["statuses"][0], evidence=[])
            for item in case.get("required_findings", [])
        ]
        replies.append(
            AIMessage(
                content="",
                tool_calls=[
                    dict(
                        name="DebriefDraft",
                        id=f"draft-{number}",
                        args=dict(items=items, findings=findings),
                    )
                ],
            )
        )
    return ToolCallingFakeModel(responses=replies)


@pytest.fixture
def offline_settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        reply=OpenAIReply(model="offline-contract"),
        paths=PathSettings(record_dir=tmp_path / "meetings", journal_dir=tmp_path / "journals"),
    )


class DebriefCallCounter(AsyncCallbackHandler):
    def __init__(self) -> None:
        self.calls = 0
        self.errors = 0
        self.raw_calls: list[dict[str, Any]] = []
        self.running: dict[Any, tuple[dict[str, Any], float]] = {}

    async def on_chat_model_start(
        self, serialized: Any, messages: Any, *, run_id: Any, **kwargs: Any
    ) -> None:
        self.calls += 1
        call = dict(
            messages=[[message.model_dump(mode="json") for message in batch] for batch in messages],
            provider_metadata=serialized.get("name") if serialized else None,
        )
        self.raw_calls.append(call)
        self.running[run_id] = (call, time.monotonic())

    async def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        call, started = self.running.pop(run_id)
        call.update(
            response=response.model_dump(mode="json", serialize_as_any=True),
            latency_ms=(time.monotonic() - started) * 1000,
            error=None,
        )

    async def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        self.errors += 1
        call, started = self.running.pop(run_id)
        call.update(latency_ms=(time.monotonic() - started) * 1000, error=type(error).__name__)


async def test_native_callback_preserves_structured_tool_arguments():
    counter = DebriefCallCounter()
    await counter.on_chat_model_start({}, [[AIMessage(content="Synthetic source")]], run_id="r1")
    message = AIMessage(
        content="",
        tool_calls=[{"name": "DebriefDraft", "id": "draft", "args": {"items": [], "findings": []}}],
    )
    await counter.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=message)]]), run_id="r1"
    )
    saved = counter.raw_calls[0]["response"]["generations"][0][0]["message"]
    assert saved["tool_calls"] == message.tool_calls


def _write_json_fresh(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _stage_case(
    settings: Settings, directory: Path, fixture: dict[str, Any], case: dict[str, Any], run: int
) -> tuple[Settings, Path, Path, str]:
    staged = directory / f"{case['id']}-{run}"
    staged.mkdir()
    paths = settings.paths.model_copy(
        update={"record_dir": staged / "meetings", "journal_dir": staged / "journals"}
    )
    scoped = settings.model_copy(update={"paths": paths})
    profile = PersonaProfile.model_validate(fixture["persona"])
    persona_path = staged / "persona.json"
    persona_path.write_text(profile.model_dump_json(), encoding="utf-8")
    meeting_id = f"debrief-eval-{case['id']}-{run}"
    record = meeting_record_path(paths.record_dir, meeting_id)
    record.parent.mkdir(parents=True)
    record.write_bytes(record_bytes(case))
    write_status(
        status_path(record),
        IngestionStatus(
            meeting_id=meeting_id, group_id=profile.memory_group, status="not_requested"
        ),
    )
    brief = _case_brief(fixture, case, meeting_id)
    if brief is not None:
        write_meeting_context(
            context_path(record),
            MeetingContext(
                schema_version="meeting_context/v1",
                brief=brief,
                brief_sha256=brief_digest(brief),
                persona_sha256=persona_digest(profile),
            ),
        )
    return scoped, persona_path, record, meeting_id


def _provider_metadata(settings: Settings) -> dict[str, Any]:
    def role(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return {
            name: getattr(value, name)
            for name in ("provider", "model", "timeout_seconds", "max_retries")
        }

    return dict(
        primary=role(settings.debrief_role),
        fallback=role(settings.reply_fallback),
        response_timeout_seconds=settings.conversation.response_timeout_seconds,
    )


async def replay_debriefs(
    settings: Settings,
    directory: Path,
    *,
    model_factory: Callable[[dict[str, Any]], BaseChatModel] | None = None,
    runs: int = 1,
) -> dict[str, Any]:
    if runs < 1:
        raise ValueError("debrief eval runs must be positive")
    await asyncio.to_thread(_prepare_directory, directory)
    fixture = load_debrief_fixture()
    candidate = capture_debrief_candidate()
    _write_json_fresh(directory / "fixture.json", fixture)
    _write_json_fresh(directory / "candidate-start.json", candidate)
    results = []
    for case in fixture["cases"]:
        for run in range(runs):
            scoped, persona_path, record, meeting_id = _stage_case(
                settings, directory, fixture, case, run
            )
            counter = DebriefCallCounter()
            token = var_child_runnable_config.set({"callbacks": [counter]})
            started = time.monotonic()
            error = None
            with ExitStack() as stack:
                if model_factory is not None:
                    if case.get("expect") == "preprovider_failure":

                        def refused_provider(*args: Any, **kwargs: Any) -> BaseChatModel:
                            raise AssertionError("invalid source reached provider construction")

                        stack.enter_context(
                            patch.object(cli_debrief, "create_debrief_chat_model", refused_provider)
                        )
                    else:
                        model = model_factory(case)
                        stack.enter_context(
                            patch.object(
                                cli_debrief, "create_debrief_chat_model", return_value=model
                            )
                        )
                    stack.enter_context(
                        patch.object(cli_debrief, "create_fallback_chat_model", return_value=None)
                    )
                try:
                    await cli_debrief.debrief_meeting(
                        meeting_id=meeting_id,
                        persona_path=persona_path,
                        owner_id=fixture["persona"]["owner_id"],
                        twin_id=fixture["persona"]["twin_id"],
                        force=False,
                        settings=scoped,
                    )
                except Exception as failure:
                    error = type(failure).__name__
                finally:
                    var_child_runnable_config.reset(token)
            report_path = record.parent / "debrief.json"
            report_json = report_path.read_text() if report_path.exists() else None
            expected_failure = (
                case.get("expect") == "preprovider_failure"
                and error == "DebriefValidationError"
                and counter.calls == 0
                and report_json is None
            )
            result = dict(
                case_id=case["id"],
                run=run,
                meeting_id=meeting_id,
                status="expected_failure"
                if expected_failure
                else "failed"
                if error
                else "completed",
                error=error,
                latency_ms=(time.monotonic() - started) * 1000,
                model_calls=counter.calls,
                provider_errors=counter.errors,
                raw_calls=counter.raw_calls,
                record_sha256=hashlib.sha256(record.read_bytes()).hexdigest(),
                raw_record=record.read_text(),
                report_json=report_json,
                report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest()
                if report_json
                else None,
                provider=_provider_metadata(scoped),
                context_json=context_path(record).read_text()
                if context_path(record).exists()
                else None,
            )
            if report_json is not None:
                report = json.loads(report_json)
                _write_json_fresh(
                    record.parent / "draft.json",
                    {"items": report["items"], "findings": report["findings"]},
                )
            _write_json_fresh(directory / f"{case['id']}-{run}" / "result.json", result)
            results.append(result)
    candidate = finish_candidate(candidate)
    _write_json_fresh(directory / "results.json", results)
    hashes = await asyncio.to_thread(_artifact_hashes, directory)
    summary = dict(
        schema="debrief_eval_manifest/v1",
        mode="live" if model_factory is None else "offline_contract",
        candidate=candidate,
        runs=runs,
        results=results,
        raw_artifact_hashes=hashes,
        review_status="pending",
        semantic_pass=False,
        extraction_completed=sum(item["status"] == "completed" for item in results),
        expected_source_failures=sum(item["status"] == "expected_failure" for item in results),
        unexpected_errors=sum(item["status"] == "failed" for item in results),
        provider_errors=sum(item["provider_errors"] for item in results),
        model_calls=sum(item["model_calls"] for item in results),
        latencies_ms=[item["latency_ms"] for item in results],
        provider=_provider_metadata(settings),
        review_contract=DebriefReview.model_json_schema(),
        coverage_threshold=0.9,
    )
    _write_json_fresh(directory / "manifest.json", summary)
    await asyncio.to_thread(_freeze_artifacts, directory)
    return summary


def review_debrief_artifacts(
    directory: Path, reviews_path: Path, output_path: Path
) -> dict[str, Any]:
    manifest = json.loads((directory / "manifest.json").read_text())
    fixture = json.loads((directory / "fixture.json").read_text())
    reviews = json.loads(reviews_path.read_text())
    results = json.loads((directory / "results.json").read_text())
    results_match = results == manifest["results"]
    intact = all(
        (directory / name).is_file()
        and hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
        for name, digest in manifest["raw_artifact_hashes"].items()
    )
    accepted = (
        intact
        and results_match
        and manifest["candidate"]["unchanged"] is True
        and debrief_gate(reviews, results, fixture, runs=manifest["runs"], mode=manifest["mode"])
    )
    result = dict(
        schema="debrief_eval_review/v1",
        reviewed_at=datetime.now(UTC).isoformat(),
        raw_artifacts_intact=intact,
        results_match=results_match,
        candidate_unchanged=manifest["candidate"]["unchanged"],
        review_status="accepted" if accepted else "rejected",
        semantic_pass=accepted,
        review_sha256=hashlib.sha256(reviews_path.read_bytes()).hexdigest(),
        manifest_sha256=hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest(),
        coverage_threshold=0.9,
    )
    _write_json_fresh(output_path, result)
    return result


@pytest.mark.live
async def test_live_debrief_native_cli_corpus(tmp_path: Path) -> None:
    configured = os.environ.get("DEBRIEF_ARTIFACT_DIR")
    directory = Path(configured) if configured else tmp_path / "debrief-live"
    outcome = await replay_debriefs(Settings(), directory)
    assert outcome["candidate"]["unchanged"], outcome["candidate"]["changed_files"]
    assert outcome["extraction_completed"] == 7
    assert outcome["expected_source_failures"] == 1
    assert outcome["unexpected_errors"] == 0
    reviews = os.environ.get("DEBRIEF_REVIEWS_PATH")
    if reviews:
        result = review_debrief_artifacts(
            directory, Path(reviews), directory.parent / f"{directory.name}-review.json"
        )
        assert result["semantic_pass"], result
    else:
        assert outcome["review_status"] == "pending"
        print(
            f"Raw native debrief artifacts: {directory}; "
            "independent semantic review remains pending."
        )


INVALID_SOURCE_CODES = {
    "missing": ErrorCode.DEBRIEF_RECORD_MISSING,
    "corrupt": ErrorCode.DEBRIEF_RECORD_INVALID_LINE,
    "oversized_entry": ErrorCode.DEBRIEF_SOURCE_EXCEEDS_CHUNK,
    "too_many_chunks": ErrorCode.DEBRIEF_TOO_MANY_CHUNKS,
    "lock": ErrorCode.RECORD_LOCKED,
    "corrupt_context": ErrorCode.DEBRIEF_CONTEXT_INVALID,
}


@pytest.mark.parametrize(
    "invalid",
    ["missing", "corrupt", "oversized_entry", "too_many_chunks", "lock", "corrupt_context"],
)
async def test_cli_invalid_sources_fail_before_provider_construction(
    offline_settings, tmp_path, invalid
):
    fixture = load_debrief_fixture()
    case = fixture["cases"][0]
    directory = tmp_path / invalid
    directory.mkdir()
    scoped, persona, record, meeting_id = _stage_case(offline_settings, directory, fixture, case, 0)
    if invalid == "missing":
        record.unlink()
    elif invalid == "corrupt":
        record.write_text('{"kind":')
    elif invalid == "oversized_entry":
        entry = {**case["entries"][0], "text": "x" * 16001}
        record.write_text(json.dumps(entry) + "\n")
    elif invalid == "too_many_chunks":
        record.write_text(
            "".join(
                json.dumps({**case["entries"][0], "id": f"limit-{index}"}) + "\n"
                for index in range(725)
            )
        )
    elif invalid == "corrupt_context":
        context_path(record).write_text("{}")

    def provider_forbidden(*args: Any, **kwargs: Any) -> BaseChatModel:
        pytest.fail("invalid source reached provider construction")

    with (
        patch.object(cli_debrief, "create_debrief_chat_model", provider_forbidden),
        patch.object(cli_debrief, "create_fallback_chat_model", provider_forbidden),
        record_lock(record, purpose="meeting") if invalid == "lock" else nullcontext(),
        pytest.raises(FakeYouError) as caught,
    ):
        await cli_debrief.debrief_meeting(
            meeting_id=meeting_id,
            persona_path=persona,
            owner_id=fixture["persona"]["owner_id"],
            twin_id=fixture["persona"]["twin_id"],
            force=False,
            settings=scoped,
        )
    assert caught.value.code is INVALID_SOURCE_CODES[invalid]
    assert not (record.parent / "debrief.json").exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "malformed",
        "duplicate_item_review",
        "missing_item_review",
        "missing_finding_review",
        "unsupported",
        "false_approval",
        "unknown_identity",
        "external_action",
        "bad_criterion",
        "wrong_item_mapping",
        "wrong_quote",
        "empty_rationale",
    ],
)
def test_gate_rejects_review_and_coverage_contract_failures(mutation):
    fixture = load_debrief_fixture()
    results = contract_results(fixture)
    reviews = contract_reviews(fixture, results)
    if mutation == "malformed":
        reviews[0]["independent"] = "yes"
    elif mutation == "duplicate_item_review":
        reviews[0]["all_items"].append(reviews[0]["all_items"][0])
    elif mutation == "missing_item_review":
        reviews[0]["all_items"].pop()
    elif mutation == "missing_finding_review":
        reviews[4]["all_findings"].pop()
    elif mutation == "unsupported":
        reviews[0]["all_items"][0]["supported"] = False
    elif mutation == "false_approval":
        reviews[1]["owner_approval_supported"] = False
    elif mutation == "unknown_identity":
        reviews[1]["unknown_preserved"] = False
    elif mutation == "external_action":
        reviews[4]["no_external_action"] = False
    elif mutation == "bad_criterion":
        reviews[0]["coverage"][0]["criterion_id"] = "invented"
    elif mutation == "wrong_item_mapping":
        reviews[0]["coverage"][0]["item_indices"] = [1]
    elif mutation == "wrong_quote":
        payload = json.loads(results[0]["report_json"])
        payload["items"][0]["evidence"][0]["quote"] = "not present in the source"
        _replace_report_for_contract(results, reviews, 0, payload)
    elif mutation == "empty_rationale":
        reviews[0]["all_items"][0]["rationale"] = " "
    assert not debrief_gate(reviews, results, fixture)


def _replace_report_for_contract(
    results: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    index: int,
    payload: dict[str, Any],
) -> None:
    raw = json.dumps(payload)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    results[index].update(report_json=raw, report_sha256=digest)
    reviews[index]["report_sha256"] = digest


def test_required_item_coverage_uses_ground_truth_denominator():
    fixture = load_debrief_fixture()
    results = contract_results(fixture)
    reviews = contract_reviews(fixture, results)
    reviews[0]["coverage"].pop()
    assert debrief_gate(reviews, results, fixture)
    reviews[0]["coverage"].pop()
    assert not debrief_gate(reviews, results, fixture)


async def test_saved_raw_evidence_tampering_fails_independent_review(offline_settings, tmp_path):
    directory = tmp_path / "raw-review"
    outcome = await replay_debriefs(
        offline_settings, directory, model_factory=contract_debrief_model
    )
    fixture = load_debrief_fixture()
    reviews_path = tmp_path / "reviews.json"
    _write_json_fresh(reviews_path, contract_reviews(fixture, outcome["results"]))
    accepted = review_debrief_artifacts(directory, reviews_path, tmp_path / "accepted.json")
    assert accepted["semantic_pass"] is True
    record = next(directory.rglob("record.jsonl"))
    record.chmod(0o644)
    record.write_text(record.read_text() + "\n")
    rejected = review_debrief_artifacts(directory, reviews_path, tmp_path / "rejected.json")
    assert rejected["semantic_pass"] is False
    assert rejected["raw_artifacts_intact"] is False


async def test_manifest_result_override_cannot_replace_hashed_outputs(offline_settings, tmp_path):
    directory = tmp_path / "raw-manifest-review"
    outcome = await replay_debriefs(
        offline_settings, directory, model_factory=contract_debrief_model
    )
    reviews_path = tmp_path / "reviews.json"
    _write_json_fresh(reviews_path, contract_reviews(load_debrief_fixture(), outcome["results"]))
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["results"][0]["latency_ms"] += 1
    manifest_path.chmod(0o644)
    manifest_path.write_text(json.dumps(manifest))
    result = review_debrief_artifacts(directory, reviews_path, tmp_path / "rejected.json")
    assert result["semantic_pass"] is False
    assert result["results_match"] is False


def _prepare_directory(directory: Path) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(exist_ok=False)


def _artifact_hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _freeze_artifacts(directory: Path) -> None:
    for path in directory.rglob("*"):
        if path.is_file():
            path.chmod(0o444)


def _fixture_context_text(
    fixture: dict[str, Any], case: dict[str, Any], meeting_id: str
) -> str | None:
    brief = _case_brief(fixture, case, meeting_id)
    if brief is None:
        return None
    profile = PersonaProfile.model_validate(fixture["persona"])
    context = MeetingContext(
        schema_version="meeting_context/v1",
        brief=brief,
        brief_sha256=brief_digest(brief),
        persona_sha256=persona_digest(profile),
    )
    return context.model_dump_json(indent=2) + "\n"


def test_offline_contract_reviews_cannot_approve_live_artifacts():
    fixture = load_debrief_fixture()
    results = contract_results(fixture)
    reviews = contract_reviews(fixture, results)
    assert not debrief_gate(reviews, results, fixture, mode="live")
