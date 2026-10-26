import json

import pytest

from fakeyou.conversation.contribution import (
    ContributionClaim,
    ContributionGate,
    source_registry,
    validate_contribution,
)
from fakeyou.conversation.reply_planner import PlanRead, ReplyHandlers, ReplyPlanner, planned_reply

TRIGGER = "The shared screen changed to Launch Timeline: beta slips to 20 October"


def _gate(memory: dict[str, str] | None = None) -> ContributionGate:
    registry = source_registry(
        profile_facts=["The Orchid beta launch is planned for 14 October 2026"],
        brief_facts={"demo-date": "The synthetic Orchid demo is booked for Friday."},
        brief_questions={"vendor-needs": "Which synthetic vendor input is missing?"},
        brief_objectives={"blockers": "Find the synthetic kickoff blocker."},
    )
    return ContributionGate(
        registry=registry, trigger_text=TRIGGER, memory_refs=lambda: memory or {}
    )


def _validate(**changes: object) -> tuple[bool, str]:
    values: dict[str, object] = {
        "structured": True,
        "intent": "answer",
        "text": "The beta actually launches on 14 October.",
        "kind": "correction",
        "source": "profile",
        "source_ref": "fact-0",
        "gate": _gate(),
    }
    values.update(changes)
    gate = values.pop("gate")
    claim = ContributionClaim(
        structured=bool(values["structured"]),
        intent=str(values["intent"]),
        text=str(values["text"]),
        kind=values["kind"] if isinstance(values["kind"], str) else None,
        source=values["source"] if isinstance(values["source"], str) else None,
        source_ref=values["source_ref"] if isinstance(values["source_ref"], str) else None,
    )
    verdict = validate_contribution(claim, gate) if isinstance(gate, ContributionGate) else None
    assert verdict is not None
    return verdict.passed, verdict.reason


def test_a_valid_correction_passes() -> None:
    assert _validate() == (True, "")


def test_a_valid_question_from_the_brief_passes() -> None:
    assert _validate(
        text="Which synthetic vendor input is still missing?",
        kind="question",
        source="brief",
        source_ref="vendor-needs",
    ) == (True, "")


def test_silence_always_passes() -> None:
    assert _validate(intent="silent", text="", kind=None, source=None, source_ref=None) == (
        True,
        "",
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"structured": False}, "unstructured"),
        ({"kind": None}, "missing_kind_source"),
        ({"source": None}, "missing_kind_source"),
        ({"source_ref": None}, "missing_kind_source"),
        ({"kind": "opinion"}, "missing_kind_source"),
        ({"source_ref": "fact-9"}, "unknown_source_ref"),
        ({"source": "memory", "source_ref": "fact-0"}, "unknown_source_ref"),
        ({"kind": "question", "source": "profile", "source_ref": "fact-0"}, "kind_source_mismatch"),
        (
            {"kind": "question", "source": "brief", "source_ref": "demo-date"},
            "kind_source_mismatch",
        ),
    ],
)
def test_each_drop_rule_reports_its_reason(changes: dict[str, object], reason: str) -> None:
    assert _validate(**changes) == (False, reason)


def test_a_correction_from_memory_is_a_mismatch() -> None:
    gate = _gate(memory={"edge-1": "The beta launch is planned for 14 October 2026"})
    assert _validate(source="memory", source_ref="edge-1", gate=gate) == (
        False,
        "kind_source_mismatch",
    )


def test_a_fact_from_memory_recalled_this_turn_passes() -> None:
    gate = _gate(memory={"edge-1": "The beta launch is planned for 14 October 2026"})
    assert _validate(kind="fact", source="memory", source_ref="edge-1", gate=gate) == (True, "")


def _plan(text: str, **extra: object) -> str:
    return json.dumps(
        {
            "understood": "screen changed",
            "language": "en",
            "intent": "answer",
            "text": text,
            **extra,
        }
    )


def test_the_planner_gates_on_the_final_text_only() -> None:
    reads: list[PlanRead] = []
    planner = ReplyPlanner(
        max_sentences=2,
        language="en",
        handlers=ReplyHandlers(on_plan=reads.append),
        contribution_gate=_gate(),
    )
    raw = _plan(
        "Got it — the beta actually launches on 14 October.",
        contribution_kind="correction",
        contribution_source="profile",
        source_ref="fact-0",
    )
    released = ""
    for index in range(0, len(raw), 3):
        released += planner.feed(raw[index : index + 3])
    assert released == ""
    released += planner.finish()

    assert "14 October" in released
    assert [read.gate for read in reads] == ["pass"]


def test_the_planner_drops_a_sourceless_contribution() -> None:
    reads: list[PlanRead] = []
    planner = ReplyPlanner(
        max_sentences=2,
        language="en",
        handlers=ReplyHandlers(on_plan=reads.append),
        contribution_gate=_gate(),
    )
    released = planner.feed(_plan("The launch timeline looks great.")) + planner.finish()

    assert released == ""
    assert planner.silent
    assert [(read.gate, read.gate_reason) for read in reads] == [("drop", "missing_kind_source")]


def test_an_ack_intent_is_gated_too() -> None:
    planner = ReplyPlanner(max_sentences=2, language="en", contribution_gate=_gate())
    raw = json.dumps({"understood": "screen", "language": "en", "intent": "ack", "text": "Got it."})

    assert planner.feed(raw) + planner.finish() == ""
    assert planner.silent


def test_unstructured_text_is_dropped_on_the_gated_lane() -> None:
    planner = ReplyPlanner(max_sentences=2, language="en", contribution_gate=_gate())

    assert planner.feed("Nice slide.") + planner.finish() == ""
    assert planner.silent


def test_the_whole_reply_path_is_gated_identically() -> None:
    dropped = planned_reply(
        _plan("The launch timeline looks great."),
        max_sentences=2,
        language="en",
        contribution_gate=_gate(),
    )
    kept = planned_reply(
        _plan(
            "The beta actually launches on 14 October.",
            contribution_kind="correction",
            contribution_source="profile",
            source_ref="fact-0",
        ),
        max_sentences=2,
        language="en",
        contribution_gate=_gate(),
    )

    assert dropped.text == ""
    assert kept.text == "The beta actually launches on 14 October."


def test_without_a_gate_the_planner_is_unchanged() -> None:
    planner = ReplyPlanner(max_sentences=2, language="en")

    assert planner.feed(_plan("Hello there.")) + planner.finish() == "Hello there."
