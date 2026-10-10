from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.orchestration.lifecycle import (
    MeetingLifecycle,
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


@pytest.mark.asyncio
async def test_lifecycle_state_survives_postgres_reopen(migrated_database_url: str) -> None:
    config = {"configurable": {"thread_id": "durable-lifecycle"}}
    async with open_checkpointer(migrated_database_url) as saver:
        first = create_lifecycle_graph(checkpointer=saver)
        await first.ainvoke(
            {
                **initial_lifecycle_state("owner-1", "twin-1", "meeting-1"),
                "event": _event("join_requested"),
            },
            config,
        )
        await first.ainvoke({"event": _event("admitted", "host_action")}, config)

    async with open_checkpointer(migrated_database_url) as saver:
        second = create_lifecycle_graph(checkpointer=saver)
        reopened = await second.aget_state(config)

        assert reopened.values["phase"] == "admitted"
        await second.ainvoke({"event": _event("owner_stopped")}, config)
        result = await second.ainvoke({"event": _event("leave_sent")}, config)

    assert result["phase"] == "ended"
    assert result["terminal_reason"] == "stopped_by_owner"
    assert result["external_effects"] == ["join_requested", "leave_requested"]


def test_meeting_thread_id_is_deterministic() -> None:
    assert meeting_thread_id("twin-1", "meeting-1") == "twin-1:meeting-1"


async def test_lifecycle_resume_restarts_a_crashed_thread_in_place() -> None:
    saver = InMemorySaver()
    first = MeetingLifecycle("owner-1", "twin-1", "meeting-1", saver)
    await first.initialize()
    await first.send("join_requested", "owner_action", "test")
    await first.send("admitted", "host_action", "test")
    assert first.state["phase"] == "admitted"

    resumed = MeetingLifecycle("owner-1", "twin-1", "meeting-1", saver)
    with pytest.raises(FakeYouError, match="cannot resume meeting") as caught:
        await resumed.initialize()
    assert caught.value.code is ErrorCode.MEETING_CANNOT_RESUME_PHASE

    await resumed.initialize(resume=True)

    assert resumed.state["phase"] == "preparing"
    assert resumed.state["pending_reason"] is None
    assert resumed.state["terminal_reason"] is None
    assert [row["event"] for row in resumed.state["evidence"]] == [
        "join_requested",
        "admitted",
    ]
    assert resumed.state["external_effects"] == ["join_requested"]
    assert resumed.state["owner_id"] == "owner-1"
    saved = await saver.aget_tuple(
        {"configurable": {"thread_id": meeting_thread_id("twin-1", "meeting-1")}}
    )
    assert saved is not None
    assert saved.checkpoint["channel_values"]["phase"] == "preparing"
    assert saved.checkpoint["channel_values"]["evidence"] == resumed.state["evidence"]
    assert (
        saved.checkpoint["channel_values"]["external_effects"] == resumed.state["external_effects"]
    )

    await resumed.send("join_requested", "owner_action", "retry")
    assert resumed.state["phase"] == "lobby"


async def test_lifecycle_resume_still_rejects_an_identity_mismatch() -> None:
    saver = InMemorySaver()
    first = MeetingLifecycle("owner-1", "twin-1", "meeting-1", saver)
    await first.initialize()
    await first.send("join_requested", "owner_action", "test")

    resumed = MeetingLifecycle("owner-2", "twin-1", "meeting-1", saver)
    with pytest.raises(FakeYouError, match="checkpoint identity does not match") as caught:
        await resumed.initialize(resume=True)
    assert caught.value.code is ErrorCode.MEETING_CHECKPOINT_IDENTITY_MISMATCH
