from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph

from fakeyou.lifecycle import (
    create_lifecycle_graph,
    initial_lifecycle_state,
    meeting_thread_id,
)


def _event(kind: str, attribution: str = "owner_action", detail: str = "test") -> dict[str, str]:
    return {"kind": kind, "attribution": attribution, "detail": detail}


def _start(graph: CompiledStateGraph[Any, Any, Any, Any], thread: str) -> dict[str, Any]:
    return graph.invoke(
        {**initial_lifecycle_state("owner-1", "twin-1", "meeting-1"), "event": None},
        {"configurable": {"thread_id": thread}},
    )


def _send(
    graph: CompiledStateGraph[Any, Any, Any, Any],
    thread: str,
    kind: str,
    attribution: str = "owner_action",
    detail: str = "test",
) -> dict[str, Any]:
    return graph.invoke(
        {"event": _event(kind, attribution, detail)},
        {"configurable": {"thread_id": thread}},
    )


def test_lifecycle_join_and_leave_happy_path() -> None:
    graph = create_lifecycle_graph(checkpointer=InMemorySaver())
    thread = "happy-path"
    _start(graph, thread)

    assert _send(graph, thread, "join_requested")["phase"] == "lobby"
    assert _send(graph, thread, "admitted", "host_action")["phase"] == "admitted"
    assert _send(graph, thread, "owner_stopped")["phase"] == "leaving"
    result = _send(graph, thread, "leave_sent")

    assert result["phase"] == "ended"
    assert result["terminal_reason"] == "stopped_by_owner"
    assert result["external_effects"] == ["join_requested", "leave_requested"]
    assert len(result["evidence"]) == 4
    assert result["evidence"][1]["attribution"] == "host_action"
    assert all(entry["at"] for entry in result["evidence"])


def test_lifecycle_denied_by_host() -> None:
    graph = create_lifecycle_graph(checkpointer=InMemorySaver())
    thread = "denied"
    _start(graph, thread)
    _send(graph, thread, "join_requested")

    result = _send(graph, thread, "denied", "host_action", "host declined")

    assert result["phase"] == "ended"
    assert result["terminal_reason"] == "denied_by_host"
    assert result["evidence"][-1]["detail"] == "host declined"


def test_lifecycle_platform_block_ends_session() -> None:
    graph = create_lifecycle_graph(checkpointer=InMemorySaver())
    thread = "blocked"
    _start(graph, thread)
    _send(graph, thread, "join_requested")

    result = _send(graph, thread, "blocked", "system_fault")

    assert result["phase"] == "ended"
    assert result["terminal_reason"] == "blocked_by_platform"


def test_lifecycle_lobby_timeout() -> None:
    graph = create_lifecycle_graph(checkpointer=InMemorySaver())
    thread = "lobby-timeout"
    _start(graph, thread)
    _send(graph, thread, "join_requested")

    result = _send(graph, thread, "lobby_timed_out", "system_fault")

    assert result["phase"] == "ended"
    assert result["terminal_reason"] == "lobby_timeout_reached"


def test_lifecycle_removed_by_host() -> None:
    graph = create_lifecycle_graph(checkpointer=InMemorySaver())
    thread = "removed"
    _start(graph, thread)
    _send(graph, thread, "join_requested")
    _send(graph, thread, "admitted", "host_action")

    result = _send(graph, thread, "removed", "host_action")

    assert result["phase"] == "ended"
    assert result["terminal_reason"] == "removed_by_host"


def test_lifecycle_reconnect_returns_to_previous_phase() -> None:
    graph = create_lifecycle_graph(checkpointer=InMemorySaver())
    thread = "reconnect"
    _start(graph, thread)
    _send(graph, thread, "join_requested")
    _send(graph, thread, "admitted", "host_action")

    assert _send(graph, thread, "connection_lost", "system_fault")["phase"] == "disconnected"
    result = _send(graph, thread, "reconnected", "system_fault")

    assert result["phase"] == "admitted"
    assert result["previous_phase"] is None
    assert result["terminal_reason"] is None


def test_lifecycle_duration_limit_leaves_with_reason() -> None:
    graph = create_lifecycle_graph(checkpointer=InMemorySaver())
    thread = "duration-limit"
    _start(graph, thread)
    _send(graph, thread, "join_requested")
    _send(graph, thread, "admitted", "host_action")

    leaving = _send(graph, thread, "duration_reached", "system_fault")

    assert leaving["phase"] == "leaving"
    assert leaving["pending_reason"] == "duration_limit_reached"
    result = _send(graph, thread, "leave_sent", "system_fault")

    assert result["phase"] == "ended"
    assert result["terminal_reason"] == "duration_limit_reached"


def test_lifecycle_duration_limit_ends_disconnected_session() -> None:
    graph = create_lifecycle_graph(checkpointer=InMemorySaver())
    thread = "disconnected-duration"
    _start(graph, thread)
    _send(graph, thread, "join_requested")
    _send(graph, thread, "admitted", "host_action")
    _send(graph, thread, "connection_lost", "system_fault")

    result = _send(graph, thread, "duration_reached", "system_fault")

    assert result["phase"] == "ended"
    assert result["terminal_reason"] == "duration_limit_reached"


def test_lifecycle_ended_ignores_further_events() -> None:
    graph = create_lifecycle_graph(checkpointer=InMemorySaver())
    thread = "absorbing"
    _start(graph, thread)
    _send(graph, thread, "owner_stopped")
    before = graph.get_state({"configurable": {"thread_id": thread}})

    result = _send(graph, thread, "admitted", "host_action")

    assert result["phase"] == "ended"
    assert result["terminal_reason"] == "stopped_by_owner"
    assert result["external_effects"] == before.values["external_effects"]
    assert len(result["evidence"]) == len(before.values["evidence"])


def test_lifecycle_rejects_invalid_event_for_phase() -> None:
    graph = create_lifecycle_graph(checkpointer=InMemorySaver())
    thread = "invalid"
    _start(graph, thread)

    with pytest.raises(ValueError, match="invalid lifecycle event"):
        _send(graph, thread, "admitted", "host_action")


def test_lifecycle_rejects_unknown_attribution() -> None:
    graph = create_lifecycle_graph(checkpointer=InMemorySaver())
    thread = "attribution"
    _start(graph, thread)

    with pytest.raises(ValueError, match="unknown lifecycle attribution"):
        _send(graph, thread, "join_requested", "mystery")


def test_lifecycle_state_survives_sqlite_reopen(tmp_path) -> None:
    database = tmp_path / "lifecycle.sqlite"
    config = {"configurable": {"thread_id": "durable-lifecycle"}}

    with SqliteSaver.from_conn_string(str(database)) as saver:
        first = create_lifecycle_graph(checkpointer=saver)
        first.invoke(
            {
                **initial_lifecycle_state("owner-1", "twin-1", "meeting-1"),
                "event": _event("join_requested"),
            },
            config,
        )
        first.invoke({"event": _event("admitted", "host_action")}, config)

    with SqliteSaver.from_conn_string(str(database)) as saver:
        second = create_lifecycle_graph(checkpointer=saver)
        reopened = second.get_state(config)

        assert reopened.values["phase"] == "admitted"
        second.invoke({"event": _event("owner_stopped")}, config)
        result = second.invoke({"event": _event("leave_sent")}, config)

    assert result["phase"] == "ended"
    assert result["terminal_reason"] == "stopped_by_owner"
    assert result["external_effects"] == ["join_requested", "leave_requested"]


def test_meeting_thread_id_is_deterministic() -> None:
    assert meeting_thread_id("twin-1", "meeting-1") == "twin-1:meeting-1"
