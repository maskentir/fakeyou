import asyncio
from pathlib import Path
from typing import Any, override

import pytest
from langchain_core.messages import AIMessage

import fakeyou.debrief.debrief as module
from fakeyou.catalog.errors import ErrorCode
from fakeyou.debrief.debrief_schema import (
    DebriefDraft,
    DebriefItem,
    DebriefValidationError,
    ObjectiveFinding,
)
from fakeyou.debrief.debrief_sources import RecordSnapshot, load_record_snapshot
from fakeyou.persistence.brief import MeetingBrief
from fakeyou.persistence.record import ScreenEntry
from tests._conversation_fakes import ToolCallingFakeModel
from tests._debrief_support import AT, item, ref, report_for, source
from tests._debrief_support import (
    brief as brief,
)
from tests._debrief_support import (
    record_path as record_path,
)
from tests._error_support import message_pattern


def native_response(draft: DebriefDraft | dict[str, Any]) -> AIMessage:
    payload = draft.model_dump(mode="json") if isinstance(draft, DebriefDraft) else draft
    return AIMessage(
        content="", tool_calls=[{"name": "DebriefDraft", "args": payload, "id": "draft"}]
    )


def model_for(draft: DebriefDraft) -> ToolCallingFakeModel:
    return ToolCallingFakeModel(responses=[native_response(draft)])


async def test_native_tool_output_preserves_evidence_and_fills_missing_brief(
    brief: MeetingBrief,
) -> None:
    draft = DebriefDraft(items=(item(),), findings=())
    model = model_for(draft)
    result = await module.generate_debrief(
        module.DebriefModels(model),
        RecordSnapshot("a" * 64, (source(),)),
        brief=brief,
        stop=asyncio.Event(),
    )
    assert result.items == draft.items
    assert result.findings == (
        ObjectiveFinding(objective_id="objective:o1", status="uncertain", evidence=()),
        ObjectiveFinding(objective_id="question:q1", status="uncertain", evidence=()),
    )
    assert len(model.seen) == 2


async def test_no_brief_means_empty_findings() -> None:
    result = await module.generate_debrief(
        module.DebriefModels(model_for(DebriefDraft(items=(item(),), findings=()))),
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
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_NATIVE_SCHEMA_INVALID)
    ) as caught:
        await module.generate_debrief(
            module.DebriefModels(model),
            RecordSnapshot("a" * 64, (source(),)),
            brief=None,
            stop=asyncio.Event(),
        )
    assert caught.value.code is ErrorCode.DEBRIEF_NATIVE_SCHEMA_INVALID
    assert len(model.seen) == 2


async def test_raw_text_is_never_a_debrief() -> None:
    model = ToolCallingFakeModel(responses=[AIMessage(content='{"items": [], "findings": []}')])
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_NATIVE_RESPONSE_ABSENT)
    ) as caught:
        await module.generate_debrief(
            module.DebriefModels(model),
            RecordSnapshot("a" * 64, (source(),)),
            brief=None,
            stop=asyncio.Event(),
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
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_NATIVE_RESPONSE_ABSENT)
    ) as caught:
        await module.generate_debrief(
            module.DebriefModels(model),
            RecordSnapshot("a" * 64, (source(),)),
            brief=None,
            stop=asyncio.Event(),
        )
    assert caught.value.code is ErrorCode.DEBRIEF_NATIVE_RESPONSE_ABSENT
    assert len(model.seen) <= 2


async def test_each_chunk_is_validated_before_merge() -> None:
    entries = tuple(source(i) for i in range(1, 42))
    model = ToolCallingFakeModel(
        responses=[native_response(DebriefDraft(items=(item(41),), findings=()))]
    )
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_EVIDENCE_INVALID)
    ) as caught:
        await module.generate_debrief(
            module.DebriefModels(model),
            RecordSnapshot("a" * 64, entries),
            brief=None,
            stop=asyncio.Event(),
        )
    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_INVALID
    assert model.seen


async def test_a_later_chunk_failure_never_returns_partial_draft() -> None:
    entries = tuple(source(i) for i in range(1, 42))
    model = ToolCallingFakeModel(
        responses=[
            native_response(DebriefDraft(items=(item(),), findings=())),
            AIMessage(content="Failed extraction"),
        ]
    )
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_NATIVE_RESPONSE_ABSENT)
    ) as caught:
        await module.generate_debrief(
            module.DebriefModels(model),
            RecordSnapshot("a" * 64, entries),
            brief=None,
            stop=asyncio.Event(),
        )
    assert caught.value.code is ErrorCode.DEBRIEF_NATIVE_RESPONSE_ABSENT
    assert model.seen


async def test_preexisting_stop_does_not_call_any_model() -> None:
    model = model_for(DebriefDraft(items=(), findings=()))
    stop = asyncio.Event()
    stop.set()
    with pytest.raises(module.DebriefStopRequested):
        await module.generate_debrief(
            module.DebriefModels(model),
            RecordSnapshot("a" * 64, (source(),)),
            brief=None,
            stop=stop,
        )
    assert model.seen == []


async def test_stop_cancels_pending_native_call_without_using_fallback() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingModel(ToolCallingFakeModel):
        @override
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
            module.DebriefModels(model, fallback),
            RecordSnapshot("a" * 64, (source(),)),
            brief=None,
            stop=stop,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        stop.set()
        with pytest.raises(module.DebriefStopRequested):
            await asyncio.wait_for(task, timeout=2)
        assert cancelled.is_set()
        assert fallback.seen == []
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_task_cancel_without_stop_still_raises_cancelled_error() -> None:
    entered = asyncio.Event()

    class WaitingModel(ToolCallingFakeModel):
        @override
        async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
            entered.set()
            await asyncio.Event().wait()

    model = WaitingModel(responses=[AIMessage(content="unused")])
    task = asyncio.create_task(
        module.generate_debrief(
            module.DebriefModels(model),
            RecordSnapshot("a" * 64, (source(),)),
            brief=None,
            stop=asyncio.Event(),
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_NATIVE_SCHEMA_INVALID)
    ) as caught:
        await module.generate_debrief(
            module.DebriefModels(model),
            RecordSnapshot("a" * 64, (source(),)),
            brief=None,
            stop=asyncio.Event(),
        )
    assert caught.value.code is ErrorCode.DEBRIEF_NATIVE_SCHEMA_INVALID
    assert len(model.seen) == 2


async def test_outer_cancellation_cancels_only_local_extraction_without_fallback() -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingModel(ToolCallingFakeModel):
        @override
        async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    fallback = model_for(DebriefDraft(items=(), findings=()))
    task = asyncio.create_task(
        module.generate_debrief(
            module.DebriefModels(WaitingModel(responses=[AIMessage(content="unused")]), fallback),
            RecordSnapshot("a" * 64, (source(),)),
            brief=None,
            stop=asyncio.Event(),
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
        "keep a screen or chat claim that conflicts with speech as a separate uncertain item",
    ],
)
async def test_native_prompt_carries_semantic_extraction_obligations(obligation: str) -> None:
    model = model_for(DebriefDraft(items=(), findings=()))
    await module.generate_debrief(
        module.DebriefModels(model),
        RecordSnapshot("a" * 64, (source(),)),
        brief=None,
        stop=asyncio.Event(),
    )
    prompt = model.seen[0][0].content
    assert isinstance(prompt, str)
    assert obligation in prompt
    assert len(model.seen) == 2


@pytest.mark.parametrize(
    ("entry_text", "fields"),
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
        module.DebriefModels(model),
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
        module.DebriefModels(model),
        RecordSnapshot("a" * 64, (first, second)),
        brief=None,
        stop=asyncio.Event(),
    )
    assert result.items == expected
    assert len(model.seen) == 2


async def test_debrief_cites_a_shared_screen(record_path: Path) -> None:
    screen_id = "screen:screen-1"
    caption = "Quarterly revenue grew twelve percent."
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write(
            ScreenEntry(
                at=AT,
                ref="screen-1",
                shown_at=AT,
                sha256="ab" * 32,
                presenter_id="devices/7",
                presenter="Rina",
                content="slide",
                title="Q3 revenue",
                text=caption,
            ).model_dump_json()
            + "\n"
        )

    snapshot = load_record_snapshot(record_path)
    screen = next(entry for entry in snapshot.sources if entry.id == screen_id)

    assert screen.kind == "screen"
    assert screen.text == f"Q3 revenue\n{caption}"
    cited = DebriefItem.model_validate(
        {
            "kind": "decision_claim",
            "text": "Revenue grew twelve percent.",
            "status": "stated",
            "evidence": [{"entry_id": screen_id, "quote": "revenue grew twelve percent"}],
        }
    )
    merged = await module.generate_debrief(
        module.DebriefModels(model_for(DebriefDraft(items=(cited,), findings=()))),
        snapshot,
        brief=None,
        stop=asyncio.Event(),
    )
    [kept] = merged.items
    assert kept.evidence[0].entry_id == screen_id
    assert kept.evidence[0].quote in caption
    assert len(kept.evidence[0].quote) <= 1000
    report = report_for(record_path, items=[cited])
    assert any(entry.kind == "screen" for entry in report.evidence_sources)
    rendered = module.render_debrief(report)
    assert "screen&#58;screen\\-1" in rendered
    assert "revenue grew twelve percent" in rendered
    first = DebriefItem.model_validate(
        {
            "kind": "decision_claim",
            "text": "First reading of the slide.",
            "status": "uncertain",
            "evidence": [{"entry_id": screen_id, "quote": "revenue grew"}],
        }
    )
    second = DebriefItem.model_validate(
        {
            "kind": "decision_claim",
            "text": "Second reading of the slide.",
            "status": "uncertain",
            "evidence": [{"entry_id": screen_id, "quote": "twelve percent"}],
        }
    )
    conflicts = module.merge_drafts(
        (DebriefDraft(items=(first,), findings=()), DebriefDraft(items=(second,), findings=()))
    )

    assert [entry.text for entry in conflicts.items] == [
        "First reading of the slide.",
        "Second reading of the slide.",
    ]


async def test_chunks_are_extracted_at_the_same_time() -> None:
    entries = tuple(source(i) for i in range(1, 42))
    active = 0
    peak = 0

    class SlowModel(ToolCallingFakeModel):
        @override
        async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return await super()._agenerate(*args, **kwargs)

    model = SlowModel(responses=[native_response(DebriefDraft(items=(), findings=()))])

    await module.generate_debrief(
        module.DebriefModels(model),
        RecordSnapshot("a" * 64, entries),
        brief=None,
        stop=asyncio.Event(),
    )

    assert peak == 2
