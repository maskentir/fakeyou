import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import fakeyou.debrief as module
from fakeyou.brief import BriefItem, MeetingBrief, MeetingContext, brief_digest
from fakeyou.catalog.errors import ErrorCode
from fakeyou.correction_labels import (
    correction_speaker_name,
    describe_correction_speakers,
    write_correction_labels,
)
from fakeyou.debrief_schema import (
    DebriefDraft,
    DebriefItem,
    DebriefReport,
    DebriefValidationError,
    EvidenceRef,
    EvidenceSource,
    ObjectiveFinding,
)
from fakeyou.debrief_sources import RecordSnapshot, SourceEntry, load_record_snapshot
from fakeyou.diarize_client import DiarizedTurn
from fakeyou.diarize_correction import (
    correction_digest,
    correction_uuid,
    write_correction_bundle,
)
from fakeyou.journal import Journal
from tests.fakes import ToolCallingFakeModel

AT = datetime(2026, 10, 3, 12, tzinfo=UTC)
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "correction_labels.json").read_text(encoding="utf-8")
)


def source(index: int = 1, text: str = "Ask the owner about the deadline.") -> SourceEntry:
    return SourceEntry(f"transcript:e{index}", text, "participant", "unknown", AT)


def ref(index: int = 1, quote: str = "deadline") -> EvidenceRef:
    return EvidenceRef(entry_id=f"transcript:e{index}", quote=quote)


def item(index: int = 1, **changes: Any) -> DebriefItem:
    return DebriefItem.model_validate(
        {
            "kind": "owner_decision",
            "text": "The owner must decide.",
            "status": "needs_owner",
            "evidence": [ref(index)],
        }
        | changes
    )


def native_response(draft: DebriefDraft | dict[str, Any]) -> AIMessage:
    payload = draft.model_dump(mode="json") if isinstance(draft, DebriefDraft) else draft
    return AIMessage(
        content="", tool_calls=[{"name": "DebriefDraft", "args": payload, "id": "draft"}]
    )


def model_for(draft: DebriefDraft) -> ToolCallingFakeModel:
    return ToolCallingFakeModel(responses=[native_response(draft)])


@pytest.fixture
def brief() -> MeetingBrief:
    return MeetingBrief(
        schema_version="meeting_brief/v1",
        owner_id="b2-owner",
        twin_id="b2-twin",
        meeting_id="b2-meeting",
        objectives=(BriefItem(id="o1", text="Find blockers."),),
        questions=(BriefItem(id="q1", text="Which deadline?"),),
    )


@pytest.fixture
def record_path(tmp_path: Path) -> Path:
    path = tmp_path / "b2-meeting" / "record.jsonl"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "kind": "transcript",
                "at": AT.isoformat(),
                "id": "e1",
                "spoken_at": "12:00:00",
                "source": "caption",
                "text": source().text,
                "participant_id": "unknown",
                "speaker_evidence_id": None,
            }
        )
        + "\n"
    )
    return path


def report_for(record_path: Path, **changes: Any) -> DebriefReport:
    snapshot = load_record_snapshot(record_path)
    values = {
        "schema_version": "meeting_debrief/v1",
        "owner_id": "b2-owner",
        "twin_id": "b2-twin",
        "meeting_id": "b2-meeting",
        "record_sha256": snapshot.record_sha256,
        "context_sha256": None,
        "generated_at": AT,
        "provider": "fake",
        "model": "fake-model",
        "source_count": len(snapshot.sources),
        "chunk_count": 1,
        "status": "completed",
        "review_status": "unreviewed",
        "items": [item()],
        "findings": [],
        "evidence_sources": [
            EvidenceSource(
                id=s.id, text=s.text, kind=s.kind, participant_id=s.participant_id, at=s.at
            )
            for s in snapshot.sources
        ],
        "source_limitations": ["Unknown attribution."],
    }
    return DebriefReport.model_validate(values | changes)


async def test_native_tool_output_preserves_evidence_and_fills_missing_brief(
    brief: MeetingBrief,
) -> None:
    draft = DebriefDraft(items=(item(),), findings=())
    model = model_for(draft)
    result = await module.generate_debrief(
        model, RecordSnapshot("a" * 64, (source(),)), brief=brief, stop=asyncio.Event()
    )
    assert result.items == draft.items
    assert result.findings == (
        ObjectiveFinding(objective_id="objective:o1", status="uncertain", evidence=()),
        ObjectiveFinding(objective_id="question:q1", status="uncertain", evidence=()),
    )
    assert len(model.seen) == 2


async def test_no_brief_means_empty_findings() -> None:
    result = await module.generate_debrief(
        model_for(DebriefDraft(items=(item(),), findings=())),
        RecordSnapshot("a" * 64, (source(),)),
        brief=None,
        stop=asyncio.Event(),
    )
    assert result.findings == ()


@pytest.mark.parametrize(
    "payload",
    [
        {
            "items": [
                {
                    "kind": "owner_decision",
                    "text": "A claim",
                    "status": "stated",
                    "evidence": [{"entry_id": "transcript:e1", "quote": "deadline"}],
                }
            ],
            "findings": [],
        },
        {"items": [], "findings": [], "verified": True},
    ],
)
async def test_invalid_native_schema_fails_without_repair(payload: dict[str, Any]) -> None:
    model = ToolCallingFakeModel(responses=[native_response(payload)])
    with pytest.raises(DebriefValidationError) as caught:
        await module.generate_debrief(
            model, RecordSnapshot("a" * 64, (source(),)), brief=None, stop=asyncio.Event()
        )
    assert caught.value.code is ErrorCode.DEBRIEF_NATIVE_SCHEMA_INVALID
    assert len(model.seen) == 2


async def test_raw_text_is_never_a_debrief() -> None:
    model = ToolCallingFakeModel(responses=[AIMessage(content='{"items": [], "findings": []}')])
    with pytest.raises(DebriefValidationError) as caught:
        await module.generate_debrief(
            model, RecordSnapshot("a" * 64, (source(),)), brief=None, stop=asyncio.Event()
        )
    assert caught.value.code is ErrorCode.DEBRIEF_NATIVE_RESPONSE_ABSENT
    assert len(model.seen) == 2


async def test_external_tool_call_is_not_executed() -> None:
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "contact_owner", "args": {"message": "Approve now"}, "id": "external"}
                ],
            )
        ]
    )
    with pytest.raises(DebriefValidationError) as caught:
        await module.generate_debrief(
            model, RecordSnapshot("a" * 64, (source(),)), brief=None, stop=asyncio.Event()
        )
    assert caught.value.code is ErrorCode.DEBRIEF_NATIVE_RESPONSE_ABSENT
    assert len(model.seen) <= 2


async def test_each_chunk_is_validated_before_merge() -> None:
    entries = tuple(source(i) for i in range(1, 42))
    model = ToolCallingFakeModel(
        responses=[native_response(DebriefDraft(items=(item(41),), findings=()))]
    )
    with pytest.raises(DebriefValidationError) as caught:
        await module.generate_debrief(
            model, RecordSnapshot("a" * 64, entries), brief=None, stop=asyncio.Event()
        )
    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_INVALID
    assert len(model.seen) == 2


async def test_a_later_chunk_failure_never_returns_partial_draft() -> None:
    entries = tuple(source(i) for i in range(1, 42))
    model = ToolCallingFakeModel(
        responses=[
            native_response(DebriefDraft(items=(item(),), findings=())),
            AIMessage(content="Failed extraction"),
        ]
    )
    with pytest.raises(DebriefValidationError) as caught:
        await module.generate_debrief(
            model, RecordSnapshot("a" * 64, entries), brief=None, stop=asyncio.Event()
        )
    assert caught.value.code is ErrorCode.DEBRIEF_NATIVE_RESPONSE_ABSENT
    assert len(model.seen) == 4


async def test_preexisting_stop_does_not_call_any_model() -> None:
    model = model_for(DebriefDraft(items=(), findings=()))
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(asyncio.CancelledError):
        await module.generate_debrief(
            model, RecordSnapshot("a" * 64, (source(),)), brief=None, stop=stop
        )
    assert model.seen == []


async def test_stop_cancels_pending_native_call_without_using_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingModel(ToolCallingFakeModel):
        async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    model = WaitingModel(responses=[AIMessage(content="unused")])
    fallback = model_for(DebriefDraft(items=(), findings=()))
    stop = asyncio.Event()
    task = asyncio.create_task(
        module.generate_debrief(
            model, RecordSnapshot("a" * 64, (source(),)), brief=None, stop=stop, fallback=fallback
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        stop.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert cancelled.is_set()
        assert fallback.seen == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_merge_deduplicates_only_exact_items_and_preserves_conflicts() -> None:
    first = item()
    changed = item(text="A conflicting owner request.")
    paraphrase = item(text="The owner should decide.")
    result = module.merge_drafts(
        [
            DebriefDraft(items=(first,), findings=()),
            DebriefDraft(items=(first, changed, paraphrase), findings=()),
        ]
    )
    assert result.items == (first, changed, paraphrase)


@pytest.mark.parametrize(
    "statuses,expected",
    [
        (("addressed", "uncertain"), "uncertain"),
        (("open", "addressed"), "addressed"),
        (("uncertain", "open"), "open"),
    ],
)
def test_merge_findings_uses_conservative_priority(
    statuses: tuple[str, str], expected: str
) -> None:
    evidence = ((ref(),), (ref(2),)) if statuses != ("uncertain", "open") else ((), ())
    findings = [
        ObjectiveFinding.model_validate(
            {"objective_id": "objective:o1", "status": state, "evidence": refs}
        )
        for state, refs in zip(statuses, evidence, strict=True)
    ]
    result = module.merge_drafts(
        [DebriefDraft(items=(), findings=(finding,)) for finding in findings]
    )
    assert result.findings[0].status == expected
    assert result.findings[0].evidence == tuple(dict.fromkeys(e for refs in evidence for e in refs))


def test_merge_rejects_more_than_100_distinct_items() -> None:
    drafts = [
        DebriefDraft(items=tuple(item(text=f"Claim {i}") for i in range(100)), findings=()),
        DebriefDraft(items=(item(text="Overflow"),), findings=()),
    ]
    with pytest.raises(DebriefValidationError, match="100") as caught:
        module.merge_drafts(drafts)
    assert caught.value.code is ErrorCode.DEBRIEF_MERGED_TOO_MANY_ITEMS


def test_merge_rejects_evidence_overflow_instead_of_dropping_sources() -> None:
    drafts = [
        DebriefDraft(
            items=(),
            findings=(
                ObjectiveFinding(
                    objective_id="objective:o1",
                    status="addressed",
                    evidence=tuple(ref(i) for i in range(start, start + 6)),
                ),
            ),
        )
        for start in (1, 7)
    ]
    with pytest.raises(DebriefValidationError, match="10") as caught:
        module.merge_drafts(drafts)
    assert caught.value.code is ErrorCode.DEBRIEF_MERGED_FINDING_TOO_MANY_REFS


def test_render_preserves_source_metadata_and_escapes_participant_markup(record_path: Path) -> None:
    report = report_for(
        record_path,
        items=[
            item(
                text="[contact](javascript:alert(1)) <script>x</script>",
                assignee_claim="Someone",
                due_text="tomorrow",
            )
        ],
    )
    rendered = module.render_debrief(report)
    assert "unknown" in rendered
    assert "unreviewed" in rendered
    assert "needs_owner" in rendered
    assert report.record_sha256 in rendered
    assert "tomorrow" in rendered
    assert "<script>" not in rendered
    assert "[contact](javascript:" not in rendered
    assert "2026" in rendered


def test_publish_json_last_and_rebuild_markdown_from_canonical_report(
    record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = report_for(record_path)
    written: list[str] = []
    real_write = module.write_atomically

    def tracked(path: Path, data: bytes) -> None:
        written.append(path.name)
        real_write(path, data)

    monkeypatch.setattr(module, "write_atomically", tracked)
    module.write_debrief(record_path, report)
    assert written == ["debrief.md", "debrief.json"]
    assert module.read_debrief(record_path) == report
    markdown = record_path.parent / "debrief.md"
    markdown.unlink()
    module.rebuild_debrief_markdown(record_path)
    assert markdown.read_text() == module.render_debrief(report)


@pytest.mark.parametrize("failure_name", ["debrief.md", "debrief.json"])
def test_atomic_publish_failure_preserves_previous_canonical_report(
    record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_name: str,
) -> None:
    previous = report_for(record_path)
    module.write_debrief(record_path, previous)
    canonical = record_path.parent / "debrief.json"
    before = canonical.read_bytes()
    real_write = module.write_atomically

    def failing(path: Path, data: bytes) -> None:
        if path.name == failure_name:
            raise OSError("synthetic write failure")
        real_write(path, data)

    monkeypatch.setattr(module, "write_atomically", failing)
    with pytest.raises(OSError):
        module.write_debrief(record_path, report_for(record_path, model="new-model"))
    assert canonical.read_bytes() == before
    assert module.debrief_report_status(record_path) == "available"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("record", ErrorCode.DEBRIEF_SOURCE_HASH_CHANGED),
        ("context", ErrorCode.DEBRIEF_CONTEXT_INVALID),
    ],
)
def test_publication_checks_freshness_before_replacing_previous_report(
    record_path: Path,
    change: str,
    code: ErrorCode,
) -> None:
    report = report_for(record_path)
    module.write_debrief(record_path, report)
    canonical = record_path.parent / "debrief.json"
    before = canonical.read_bytes()
    if change == "record":
        record_path.write_bytes(record_path.read_bytes() + b"\n")
    else:
        (record_path.parent / "meeting-context.json").write_text("{}")
    with pytest.raises(DebriefValidationError) as caught:
        module.write_debrief(record_path, report)
    assert caught.value.code is code
    assert canonical.read_bytes() == before


def test_publication_rejects_fabricated_source_metadata(record_path: Path) -> None:
    report = report_for(record_path)
    wrong = report.evidence_sources[0].model_copy(update={"participant_id": "owner"})
    with pytest.raises(DebriefValidationError) as caught:
        module.write_debrief(record_path, report.model_copy(update={"evidence_sources": (wrong,)}))
    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_METADATA_DIFFERS
    assert not (record_path.parent / "debrief.json").exists()


def test_report_status_checks_same_byte_record_and_context_hashes(
    record_path: Path,
    brief: MeetingBrief,
) -> None:
    context = MeetingContext(
        schema_version="meeting_context/v1",
        brief=brief,
        brief_sha256=brief_digest(brief),
        persona_sha256="b" * 64,
    )
    context_path = record_path.parent / "meeting-context.json"
    context_path.write_text(context.model_dump_json())
    report = report_for(
        record_path,
        context_sha256=hashlib.sha256(context_path.read_bytes()).hexdigest(),
        findings=[
            ObjectiveFinding(objective_id="objective:o1", status="uncertain", evidence=()),
            ObjectiveFinding(objective_id="question:q1", status="uncertain", evidence=()),
        ],
    )
    assert module.debrief_report_status(record_path) == "missing"
    module.write_debrief(record_path, report)
    assert module.debrief_report_status(record_path) == "available"
    context_path.write_bytes(context_path.read_bytes() + b"\n")
    assert module.debrief_report_status(record_path) == "stale"
    context_path.unlink()
    assert module.debrief_report_status(record_path) == "stale"


@pytest.mark.parametrize("raw", ["partial", "{}", '{"schema_version":"meeting_debrief/v99"}'])
def test_corrupt_canonical_report_is_invalid(record_path: Path, raw: str) -> None:
    (record_path.parent / "debrief.json").write_text(raw)
    assert module.debrief_report_status(record_path) == "invalid"


@pytest.mark.parametrize("previous_exists", [True, False])
def test_post_replace_write_failure_restores_previous_canonical_state(
    record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous_exists: bool,
) -> None:
    canonical = record_path.parent / "debrief.json"
    previous = report_for(record_path)
    if previous_exists:
        module.write_debrief(record_path, previous)
    before = canonical.read_bytes() if previous_exists else None
    real_write = module.write_atomically

    def failing_after_replace(path: Path, data: bytes) -> None:
        real_write(path, data)
        if path.name == "debrief.json":
            raise OSError("synthetic post-replace directory sync failure")

    monkeypatch.setattr(module, "write_atomically", failing_after_replace)
    with pytest.raises(OSError):
        module.write_debrief(record_path, report_for(record_path, model="new-model"))
    assert (canonical.read_bytes() if canonical.exists() else None) == before


def test_failed_rollback_preserves_old_backup_and_blocks_cache(
    record_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = report_for(record_path)
    module.write_debrief(record_path, previous)
    canonical = record_path.parent / "debrief.json"
    before = canonical.read_bytes()
    real_write = module.write_atomically
    real_replace = module.os.replace

    def failing_write(path: Path, data: bytes) -> None:
        real_write(path, data)
        if path.name == "debrief.json":
            raise OSError("directory sync failed")

    def failing_restore(source: Path, target: Path) -> None:
        if str(source).endswith(".rollback"):
            raise OSError("rollback failed")
        real_replace(source, target)

    monkeypatch.setattr(module, "write_atomically", failing_write)
    monkeypatch.setattr(module.os, "replace", failing_restore)
    with pytest.raises(OSError):
        module.write_debrief(record_path, report_for(record_path, model="failed-model"))
    backups = list(record_path.parent.glob("debrief.json.*.rollback"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before
    assert module.debrief_report_status(record_path) == "invalid"
    with pytest.raises(DebriefValidationError, match="recovery") as caught:
        module.read_debrief(record_path)
    assert caught.value.code is ErrorCode.DEBRIEF_PUBLICATION_RECOVERY
    monkeypatch.setattr(module, "write_atomically", real_write)
    with pytest.raises(DebriefValidationError, match="recovery") as caught:
        module.write_debrief(record_path, previous)
    assert caught.value.code is ErrorCode.DEBRIEF_PUBLICATION_RECOVERY
    assert backups[0].read_bytes() == before


@pytest.mark.parametrize(
    "field,change",
    [
        ("items", (item(evidence=(ref(quote="Fabricated approval"),)),)),
        (
            "evidence_sources",
            (
                EvidenceSource(
                    id="transcript:e1",
                    text=source().text,
                    kind="participant",
                    participant_id="guessed-owner",
                    at=AT,
                ),
            ),
        ),
        ("evidence_sources", ()),
    ],
)
def test_cached_report_with_tampered_evidence_is_invalid(
    record_path: Path,
    field: str,
    change: Any,
) -> None:
    report = report_for(record_path)
    module.write_debrief(record_path, report)
    altered = report.model_copy(update={field: change})
    (record_path.parent / "debrief.json").write_text(altered.model_dump_json())
    assert module.debrief_report_status(record_path) == "invalid"


def test_publish_rejects_source_changed_after_markdown_write(
    record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = report_for(record_path)
    module.write_debrief(record_path, previous)
    canonical = record_path.parent / "debrief.json"
    before = canonical.read_bytes()
    real_write = module.write_atomically

    def mutate_source(path: Path, data: bytes) -> None:
        real_write(path, data)
        if path.name == "debrief.md":
            record_path.write_bytes(record_path.read_bytes() + b"\n")

    monkeypatch.setattr(module, "write_atomically", mutate_source)
    with pytest.raises(DebriefValidationError) as caught:
        module.write_debrief(record_path, report_for(record_path, model="new-model"))
    assert caught.value.code is ErrorCode.DEBRIEF_SOURCE_HASH_CHANGED
    assert canonical.read_bytes() == before


def test_publish_revalidates_bypassed_report_schema(record_path: Path) -> None:
    report = report_for(record_path).model_copy(update={"review_status": "verified"})
    with pytest.raises(ValueError):
        module.write_debrief(record_path, report)
    assert not (record_path.parent / "debrief.json").exists()


def test_renderer_distinguishes_no_findings_from_missing_brief(record_path: Path) -> None:
    report = report_for(record_path, context_sha256="b" * 64)
    assert "not_available" not in module.render_debrief(report)


async def test_multiple_native_outputs_fail_without_repair_or_text_parsing() -> None:
    response = native_response(DebriefDraft(items=(), findings=()))
    response.tool_calls.append(
        {
            "name": "DebriefDraft",
            "args": {"items": [], "findings": []},
            "id": "duplicate",
            "type": "tool_call",
        }
    )
    model = ToolCallingFakeModel(responses=[response])
    with pytest.raises(DebriefValidationError) as caught:
        await module.generate_debrief(
            model, RecordSnapshot("a" * 64, (source(),)), brief=None, stop=asyncio.Event()
        )
    assert caught.value.code is ErrorCode.DEBRIEF_NATIVE_SCHEMA_INVALID
    assert len(model.seen) == 2


async def test_outer_cancellation_cancels_only_local_extraction_without_fallback() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingModel(ToolCallingFakeModel):
        async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    fallback = model_for(DebriefDraft(items=(), findings=()))
    task = asyncio.create_task(
        module.generate_debrief(
            WaitingModel(responses=[AIMessage(content="unused")]),
            RecordSnapshot("a" * 64, (source(),)),
            brief=None,
            stop=asyncio.Event(),
            fallback=fallback,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert fallback.seen == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_cached_report_scope_must_match_historical_brief(
    record_path: Path,
    brief: MeetingBrief,
) -> None:
    context = MeetingContext(
        schema_version="meeting_context/v1",
        brief=brief,
        brief_sha256=brief_digest(brief),
        persona_sha256="b" * 64,
    )
    context_path = record_path.parent / "meeting-context.json"
    context_path.write_text(context.model_dump_json())
    report = report_for(
        record_path,
        context_sha256=hashlib.sha256(context_path.read_bytes()).hexdigest(),
        owner_id="different-owner",
        findings=[
            ObjectiveFinding(objective_id="objective:o1", status="uncertain", evidence=()),
            ObjectiveFinding(objective_id="question:q1", status="uncertain", evidence=()),
        ],
    )
    (record_path.parent / "debrief.json").write_text(report.model_dump_json())
    assert module.debrief_report_status(record_path) == "invalid"


@pytest.mark.parametrize(
    "obligation",
    [
        "No brief restricts only findings, never items.",
        "Retain uncertain claims with attribution and evidence rather than omitting them.",
        "decision_claim: stated, needs_owner, uncertain",
        "proposal: proposed, needs_owner, uncertain",
        "action_item: stated, proposed, needs_owner, uncertain",
        "open_question: stated, needs_owner, uncertain",
        "owner_decision: needs_owner, uncertain",
        "Budget, deadline, or contract approval/change requests "
        "use owner_decision with needs_owner",
        "A participant's explicit future work claim uses action_item with stated or uncertain",
        "A withdrawn or superseded proposal uses proposal with uncertain",
        "Copy relative task due wording into due_text verbatim",
        "Do not convert source capture timestamps into dates for undated or forwarded statements.",
        "conflicting visual claims remain separate uncertain items with frame evidence",
    ],
)
async def test_native_prompt_carries_semantic_extraction_obligations(obligation: str) -> None:
    model = model_for(DebriefDraft(items=(), findings=()))
    await module.generate_debrief(
        model, RecordSnapshot("a" * 64, (source(),)), brief=None, stop=asyncio.Event()
    )
    prompt = model.seen[0][0].content
    assert isinstance(prompt, str)
    assert obligation in prompt
    assert len(model.seen) == 2


@pytest.mark.parametrize(
    "entry_text,fields",
    [
        (
            "A participant claims the owner approved the plan.",
            {
                "kind": "decision_claim",
                "status": "uncertain",
                "text": "An unidentified participant alleges approval; it is not authenticated.",
            },
        ),
        (
            "We request owner approval to increase the expense limit.",
            {
                "kind": "owner_decision",
                "status": "needs_owner",
                "text": "The participant requests a local owner decision on the expense limit.",
            },
        ),
        (
            "Can the twin authorize the revised agreement?",
            {
                "kind": "owner_decision",
                "status": "needs_owner",
                "text": "Agreement authorization remains a local owner decision.",
            },
        ),
        (
            "Drew will prepare the checklist.",
            {
                "kind": "action_item",
                "status": "stated",
                "assignee_claim": "Drew",
                "text": "Drew is claimed to prepare a checklist; the work is not completed.",
            },
        ),
        (
            "Lee will publish the outline next week after breakfast; "
            "the forwarded statement is undated.",
            {
                "kind": "action_item",
                "status": "uncertain",
                "assignee_claim": "Lee",
                "due_text": "next week after breakfast",
                "text": "The participant relays planned work with an undated relative due claim.",
            },
        ),
        (
            "I retract the layout proposal; the selection remains unresolved.",
            {
                "kind": "proposal",
                "status": "uncertain",
                "text": "The participant withdrew the layout proposal; no approval is pending.",
            },
        ),
    ],
)
async def test_native_output_preserves_uncertain_authority_and_future_work_claims(
    entry_text: str,
    fields: dict[str, Any],
) -> None:
    expected = DebriefItem.model_validate(fields | {"evidence": [ref(quote=entry_text)]})
    model = model_for(DebriefDraft(items=(expected,), findings=()))
    result = await module.generate_debrief(
        model,
        RecordSnapshot("a" * 64, (source(text=entry_text),)),
        brief=None,
        stop=asyncio.Event(),
    )
    assert result.items == (expected,)
    assert result.findings == ()
    assert len(model.seen) == 2


async def test_native_conflicting_claims_remain_separate_without_authentication() -> None:
    first = source(text="I claim the team chose asynchronous delivery.")
    second = source(2, text="I disagree; I claim the team chose immediate delivery.")
    expected = (
        item(
            kind="decision_claim",
            status="uncertain",
            text="One participant reports asynchronous delivery as the decision; it is disputed.",
            evidence=(ref(quote=first.text),),
        ),
        item(
            2,
            kind="decision_claim",
            status="uncertain",
            text="A participant reports the contrary immediate delivery decision; it is disputed.",
            evidence=(ref(2, quote=second.text),),
        ),
    )
    model = model_for(DebriefDraft(items=expected, findings=()))
    result = await module.generate_debrief(
        model, RecordSnapshot("a" * 64, (first, second)), brief=None, stop=asyncio.Event()
    )
    assert result.items == expected
    assert len(model.seen) == 2


def test_sidecar_change_marks_cached_report_stale(record_path: Path) -> None:
    report = report_for(record_path)
    module.write_debrief(record_path, report)
    assert module.debrief_report_status(record_path) == "available"
    audio_dir = record_path.parent / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        record_path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: AT,
    )

    assert module.debrief_report_status(record_path) == "stale"


def test_status_check_reports_stale_sidecar_fallback_to_journal(record_path: Path) -> None:
    report = report_for(record_path)
    module.write_debrief(record_path, report)
    audio_dir = record_path.parent / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        record_path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: AT,
    )
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "kind": "transcript",
                    "at": AT.isoformat(),
                    "id": "e2",
                    "spoken_at": "12:01:00",
                    "source": "caption",
                    "text": "A second voice notes the deadline.",
                    "participant_id": "unknown",
                    "speaker_evidence_id": None,
                }
            )
            + "\n"
        )
    lines: list[str] = []
    journal = Journal("b2-meeting", emit=lines.append)

    assert module.debrief_report_status(record_path, journal=journal) == "stale"

    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "fault_recorded"
    assert row["source"] == "meeting_record"
    assert row["detail"] == "diarization sidecar is stale: record changed since correction"


@pytest.mark.parametrize("label", [FIXTURE["render_shown"], None])
def test_render_correction_uuid_uses_human_readable_unverified_speaker(
    record_path: Path, label: str | None
) -> None:
    uuid = correction_uuid("A", 0)
    renamed = (
        report_for(record_path).evidence_sources[0].model_copy(update={"participant_id": uuid})
    )
    report = report_for(record_path).model_copy(update={"evidence_sources": (renamed,)})
    rendered = module.render_debrief(
        report, speaker_names={uuid: correction_speaker_name("A", label)}
    )

    assert report.evidence_sources[0].participant_id == uuid
    if label is None:
        assert "Speaker A, unverified" in rendered
        assert "shown as" not in rendered
    else:
        assert f"Speaker A, shown as {label}, unverified" in rendered


def test_write_debrief_autoloads_correction_labels_into_markdown(record_path: Path) -> None:
    audio_dir = record_path.parent / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        record_path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: AT,
    )
    uuid = correction_uuid("A", 0)
    write_correction_labels(record_path, {uuid: FIXTURE["render_shown"]})
    report = report_for(record_path, correction_sha256=correction_digest(record_path))
    module.write_debrief(record_path, report)
    markdown = record_path.with_name(module.MARKDOWN_FILE_NAME).read_text(encoding="utf-8")
    expected = f"Speaker A, shown as {FIXTURE['render_shown']}, unverified"

    assert report.evidence_sources[0].participant_id == uuid
    assert describe_correction_speakers(record_path) == {uuid: expected}
    assert expected in markdown


async def test_debrief_cites_frame(record_path: Path) -> None:
    visual_ref = f"frame:abcd1234@{AT.isoformat()}"
    caption = "Quarterly revenue grew twelve percent."
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "kind": "visual",
                    "at": AT.isoformat(),
                    "visual_id": "frame-1",
                    "captured_at": AT.isoformat(),
                    "captured_page_ms": 1727966400000.0,
                    "sha256": "ab" * 32,
                    "caption": caption,
                    "frame_ref": visual_ref,
                }
            )
            + "\n"
        )

    snapshot = load_record_snapshot(record_path)
    visual = next(entry for entry in snapshot.sources if entry.id == visual_ref)

    assert visual.kind == "visual"
    assert visual.text == caption
    cited = DebriefItem.model_validate(
        {
            "kind": "decision_claim",
            "text": "Revenue grew twelve percent.",
            "status": "stated",
            "evidence": [{"entry_id": visual_ref, "quote": "revenue grew twelve percent"}],
        }
    )
    merged = await module.generate_debrief(
        model_for(DebriefDraft(items=(cited,), findings=())),
        snapshot,
        brief=None,
        stop=asyncio.Event(),
    )
    [kept] = merged.items
    assert kept.evidence[0].entry_id == visual_ref
    assert kept.evidence[0].quote in caption
    assert len(kept.evidence[0].quote) <= 1000
    report = report_for(record_path, items=[cited])
    assert any(entry.kind == "visual" for entry in report.evidence_sources)
    rendered = module.render_debrief(report)
    assert "frame&#58;abcd1234" in rendered
    assert "revenue grew twelve percent" in rendered
    first = DebriefItem.model_validate(
        {
            "kind": "decision_claim",
            "text": "First reading of the slide.",
            "status": "uncertain",
            "evidence": [{"entry_id": visual_ref, "quote": "revenue grew"}],
        }
    )
    second = DebriefItem.model_validate(
        {
            "kind": "decision_claim",
            "text": "Second reading of the slide.",
            "status": "uncertain",
            "evidence": [{"entry_id": visual_ref, "quote": "twelve percent"}],
        }
    )
    conflicts = module.merge_drafts(
        (DebriefDraft(items=(first,), findings=()), DebriefDraft(items=(second,), findings=()))
    )

    assert [entry.text for entry in conflicts.items] == [
        "First reading of the slide.",
        "Second reading of the slide.",
    ]
