import ast
import re
from pathlib import Path
from typing import get_args

import pytest

from fakeyou.lifecycle import LifecycleAttribution, LifecycleEventKind, TerminalReason
from fakeyou.taxonomy import EventVerb, Subsystem

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "fakeyou"
EVENT_VERBS = frozenset(get_args(EventVerb))
SUBSYSTEMS = frozenset(get_args(Subsystem))
CONTENT_FIELDS = frozenset({"text", "prompt", "transcript", "completion", "body"})
DYNAMIC_SOURCES = frozenset({"source", "fault.source"})
SOURCED_HELPERS = {
    ("audio.py", "_record"): "audio",
    ("conversation.py", "_record"): "agent",
}


def _journal_calls() -> list[tuple[str, int, str, ast.Call]]:
    calls = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr in {"record", "_record"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    calls.append((path.name, node.lineno, first.value, node))
            if node.func.attr == "timed":
                for keyword in node.keywords:
                    if keyword.arg in {"started", "finished", "failed"} and isinstance(
                        keyword.value, ast.Constant
                    ):
                        calls.append((path.name, node.lineno, keyword.value.value, node))
    return calls


JOURNAL_CALLS = _journal_calls()


def _event_id(call: tuple[str, int, str, ast.Call]) -> str:
    return f"{call[0]}:{call[1]}:{call[2]}"


def test_the_scan_finds_the_journal_events() -> None:
    assert len({event for _file, _line, event, _node in JOURNAL_CALLS}) >= 30


def test_event_verbs_include_first_pcm_emission() -> None:
    assert "emitted" in get_args(EventVerb)


@pytest.mark.parametrize("call", JOURNAL_CALLS, ids=_event_id)
def test_journal_event_names_end_with_a_controlled_past_participle(call) -> None:
    _file, _line, event, _node = call
    words = event.split("_")

    assert event == event.lower()
    assert len(words) >= 2
    assert words[-1] in EVENT_VERBS


@pytest.mark.parametrize("call", JOURNAL_CALLS, ids=_event_id)
def test_journal_events_name_a_subsystem_source(call) -> None:
    file, _line, _event, node = call
    if isinstance(node.func, ast.Attribute) and (file, node.func.attr) in SOURCED_HELPERS:
        assert SOURCED_HELPERS[(file, node.func.attr)] in SUBSYSTEMS
        return
    if file == "journal.py" and all(keyword.arg is None for keyword in node.keywords):
        return
    sources = [keyword.value for keyword in node.keywords if keyword.arg == "source"]

    assert len(sources) == 1
    [source] = sources
    if isinstance(source, ast.Constant):
        assert source.value in SUBSYSTEMS
    else:
        assert ast.unparse(source) in DYNAMIC_SOURCES


@pytest.mark.parametrize("call", JOURNAL_CALLS, ids=_event_id)
def test_journal_events_carry_no_conversation_content(call) -> None:
    _file, _line, _event, node = call

    assert not {keyword.arg for keyword in node.keywords} & CONTENT_FIELDS


def test_journal_durations_are_milliseconds() -> None:
    fields = {
        keyword.arg
        for _file, _line, _event, node in JOURNAL_CALLS
        for keyword in node.keywords
        if keyword.arg
    }

    assert not {field for field in fields if field.endswith(("_seconds", "_s", "latency"))}


LIFECYCLE_VERBS = frozenset(
    {
        "admitted",
        "blocked",
        "cancelled",
        "denied",
        "ended",
        "failed",
        "left",
        "lost",
        "out",
        "reached",
        "reconnected",
        "removed",
        "requested",
        "sent",
        "stopped",
    }
)


@pytest.mark.parametrize("kind", sorted(get_args(LifecycleEventKind)))
def test_lifecycle_event_kinds_end_with_a_past_participle(kind: str) -> None:
    assert kind.split("_")[-1] in LIFECYCLE_VERBS


@pytest.mark.parametrize("reason", sorted(get_args(TerminalReason)))
def test_terminal_reasons_belong_to_one_family(reason: str) -> None:
    assert re.fullmatch(r"[a-z]+_failure|[a-z_]+_by_(owner|host|platform)|[a-z_]+_reached", reason)


def test_lifecycle_attributions_name_who_acted() -> None:
    assert set(get_args(LifecycleAttribution)) == {
        "host_action",
        "system_fault",
        "owner_action",
        "policy",
    }


def test_meet_action_events_use_controlled_names() -> None:
    assert "observed" in get_args(EventVerb)
    calls = [call for call in JOURNAL_CALLS if call[2].startswith("meet_action_")]
    assert {call[2] for call in calls} == {
        "meet_action_requested",
        "meet_action_started",
        "meet_action_observed",
        "meet_action_finished",
        "meet_action_failed",
        "meet_action_rejected",
    }
    for _file, _line, _event, node in calls:
        sources = [keyword.value for keyword in node.keywords if keyword.arg == "source"]
        assert len(sources) == 1
        source = sources[0]
        assert isinstance(source, ast.Constant)
        assert source.value == "agent"
