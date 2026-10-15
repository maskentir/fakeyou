import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, ValidationError

from evals.runtime_evidence import candidate_files, capture_candidate, debrief_files
from fakeyou.debrief.debrief_schema import DebriefDraft, DebriefReport
from fakeyou.debrief.debrief_sources import chunk_sources, validate_draft
from fakeyou.foundation.digest import canonical_json_digest
from fakeyou.memory.persona import PersonaProfile, persona_digest
from fakeyou.persistence.brief import MeetingBrief, MeetingContext, brief_digest

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
    *debrief_files(ROOT),
    "evals/fixtures/debrief-scenarios.json",
    "evals/_debrief_support.py",
    "evals/test_debrief_fixture_eval.py",
    "evals/test_debrief_runner_eval.py",
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
        raw = case["raw_record"]
        assert isinstance(raw, str)
        return raw.encode("utf-8")
    return "".join(
        json.dumps(entry, separators=(",", ":")) + "\n" for entry in case["entries"]
    ).encode("utf-8")


def load_debrief_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    fixture: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
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
    paths = sorted(set(candidate_files(root)) | set(B_FILES))
    hashes = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        if (root / name).is_file()
        else None
        for name in paths
    }
    digest = canonical_json_digest(hashes, compact=True)
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


def contract_reviews(
    fixture: dict[str, Any], results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
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


def case_brief(
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


def source_index(case: dict[str, Any]) -> dict[str, Any]:
    from pydantic import TypeAdapter

    from fakeyou.debrief.debrief_sources import SourceEntry
    from fakeyou.persistence.record import RecordEntry, TranscriptEntry, TwinSpeechEntry

    adapter: TypeAdapter[RecordEntry] = TypeAdapter(RecordEntry)
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


def validate_report_binding(
    report: DebriefReport,
    result: dict[str, Any],
    case: dict[str, Any],
    fixture: dict[str, Any],
    sources: dict[str, Any],
) -> None:
    if report.meeting_id != f"debrief-eval-{case['id']}-{result['run']}":
        raise ValueError("report meeting mismatch")
    expected_context = fixture_context_text(fixture, case, report.meeting_id)
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


def report_for_review(
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
    sources = source_index(case)
    validate_report_binding(report, result, case, fixture, sources)
    draft = DebriefDraft(items=report.items, findings=report.findings)
    brief = case_brief(fixture, case, report.meeting_id)
    validate_draft(draft, sources=sources, brief=brief)
    validate_report_evidence(report, sources, case)
    return report


def validate_report_evidence(
    report: DebriefReport, sources: dict[str, Any], case: dict[str, Any]
) -> None:
    cited = {
        ref.entry_id
        for collection in (report.items, report.findings)
        for item in collection
        for ref in item.evidence
    }
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


def criterion_matches(criterion: dict[str, Any], report: DebriefReport, indices: list[int]) -> bool:
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


def validate_reviewed_items(review: DebriefReview, report: DebriefReport) -> None:
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


def review_matches(
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
    validate_reviewed_items(review, report)
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
        or not criterion_matches(criteria[item.criterion_id], report, item.item_indices)
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
            report = report_for_review(result, case, fixture)
            count, total = review_matches(review, result, case, report)
            covered += count
            required += total
        return required > 0 and covered / required >= 0.9
    except (ValueError, TypeError, KeyError, AttributeError, ValidationError):
        return False


def fixture_context_text(
    fixture: dict[str, Any], case: dict[str, Any], meeting_id: str
) -> str | None:
    brief = case_brief(fixture, case, meeting_id)
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
