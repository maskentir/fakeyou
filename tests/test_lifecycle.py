import asyncio
from typing import Any, override

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from psycopg import OperationalError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.observability.faults import FaultReporter
from fakeyou.orchestration.lifecycle import (
    LifecycleAttribution,
    LifecycleEvent,
    LifecycleEventKind,
    LifecyclePersistence,
    MeetingLifecycle,
    create_lifecycle_graph,
    fold_lifecycle_events,
    initial_lifecycle_state,
    meeting_thread_id,
)
from tests._persistence_fakes import RecordingJournal


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


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("audio_failed", "audio_failure"), ("conversation_failed", "conversation_failure")],
)
@pytest.mark.parametrize(
    "drive",
    [
        [],
        ["join_requested"],
        ["join_requested", ("admitted", "host_action"), "owner_stopped"],
    ],
    ids=["preparing", "lobby", "leaving"],
)
def test_startup_and_teardown_failures_end_the_session(
    kind: str, reason: str, drive: list[str | tuple[str, str]]
) -> None:
    graph = create_lifecycle_graph(checkpointer=InMemorySaver())
    thread = f"failure-{kind}-{len(drive)}"
    _start(graph, thread)
    for step in drive:
        if isinstance(step, tuple):
            _send(graph, thread, step[0], step[1])
        else:
            _send(graph, thread, step)

    result = _send(graph, thread, kind, "system_fault")

    assert result["phase"] == "ended"
    assert result["terminal_reason"] == reason


def test_lifecycle_rejects_unknown_attribution() -> None:
    graph = create_lifecycle_graph(checkpointer=InMemorySaver())
    thread = "attribution"
    _start(graph, thread)

    with pytest.raises(ValueError, match="unknown lifecycle attribution"):
        _send(graph, thread, "join_requested", "mystery")


@pytest.mark.asyncio
async def test_lifecycle_state_survives_postgres_reopen(migrated_database_url: str) -> None:
    config: RunnableConfig = {"configurable": {"thread_id": "durable-lifecycle"}}
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
    phase: str = resumed.state["phase"]
    assert phase == "lobby"


async def test_lifecycle_resume_still_rejects_an_identity_mismatch() -> None:
    saver = InMemorySaver()
    first = MeetingLifecycle("owner-1", "twin-1", "meeting-1", saver)
    await first.initialize()
    await first.send("join_requested", "owner_action", "test")

    resumed = MeetingLifecycle("owner-2", "twin-1", "meeting-1", saver)
    with pytest.raises(FakeYouError, match="checkpoint identity does not match") as caught:
        await resumed.initialize(resume=True)
    assert caught.value.code is ErrorCode.MEETING_CHECKPOINT_IDENTITY_MISMATCH


class FlakySaver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.mode = "ok"
        self.released = asyncio.Event()

    @override
    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        if self.mode == "raise":
            raise OperationalError("connection reset by peer")
        if self.mode == "hang":
            await self.released.wait()
        result = await super().aput(config, checkpoint, metadata, new_versions)
        if self.mode == "commit_then_raise":
            raise OperationalError("response lost after commit")
        return result


def _observed_lifecycle(
    saver: FlakySaver, send_timeout_seconds: float = 5.0
) -> tuple[MeetingLifecycle, Any]:
    journal = RecordingJournal("meeting-1")
    lifecycle = MeetingLifecycle(
        "owner-1",
        "twin-1",
        "meeting-1",
        saver,
        persistence=LifecyclePersistence(
            send_timeout_seconds=send_timeout_seconds,
            faults=FaultReporter(journal=journal),
            journal=journal,
        ),
    )
    return lifecycle, journal


async def test_send_maps_a_hung_checkpoint_write_to_unreachable() -> None:
    saver = FlakySaver()
    lifecycle, _journal = _observed_lifecycle(saver, send_timeout_seconds=0.05)
    await lifecycle.initialize()
    saver.mode = "hang"

    try:
        with pytest.raises(FakeYouError, match="unreachable") as caught:
            await lifecycle.send("join_requested", "owner_action", "test")
    finally:
        saver.released.set()

    assert caught.value.code is ErrorCode.DATABASE_UNREACHABLE


async def test_send_failure_before_admitted_raises_unreachable() -> None:
    saver = FlakySaver()
    lifecycle, _journal = _observed_lifecycle(saver)
    await lifecycle.initialize()
    saver.mode = "raise"

    with pytest.raises(FakeYouError, match="unreachable") as caught:
        await lifecycle.send("join_requested", "owner_action", "test")

    assert caught.value.code is ErrorCode.DATABASE_UNREACHABLE
    assert lifecycle.state["phase"] == "preparing"


async def _persisted_evidence(saver: FlakySaver) -> list[str]:
    saved = await saver.aget_tuple(
        {"configurable": {"thread_id": meeting_thread_id("twin-1", "meeting-1")}}
    )
    assert saved is not None
    return [entry["event"] for entry in saved.checkpoint["channel_values"]["evidence"]]


def _evidence(lifecycle: MeetingLifecycle) -> list[str]:
    return [entry["event"] for entry in lifecycle.state["evidence"]]


async def _admit_lifecycle(saver: FlakySaver) -> MeetingLifecycle:
    lifecycle, _journal = _observed_lifecycle(saver)
    await lifecycle.initialize()
    await lifecycle.send("join_requested", "owner_action", "test")
    await lifecycle.send("admitted", "host_action", "test")
    assert lifecycle.state["phase"] == "admitted"
    return lifecycle


async def test_send_retry_adopts_an_already_applied_event() -> None:
    saver = FlakySaver()
    lifecycle = await _admit_lifecycle(saver)
    saver.mode = "commit_then_raise"

    await lifecycle.send("owner_stopped", "owner_action", "test")
    saver.mode = "ok"

    await lifecycle.send("leave_sent", "owner_action", "test")

    assert lifecycle.state["phase"] == "ended"
    assert lifecycle.state["terminal_reason"] == "stopped_by_owner"
    assert await _persisted_evidence(saver) == _evidence(lifecycle)
    kinds = [entry["event"] for entry in lifecycle.state["evidence"]]
    assert kinds.count("owner_stopped") == 1


async def test_send_retry_drops_an_event_with_no_forward_transition() -> None:
    saver = FlakySaver()
    lifecycle, _journal = _observed_lifecycle(saver)
    await lifecycle.initialize()
    await lifecycle.send("join_requested", "owner_action", "test")
    saver.mode = "commit_then_raise"

    with pytest.raises(FakeYouError, match="unreachable"):
        await lifecycle.send("admitted", "host_action", "test")
    saver.mode = "ok"

    await lifecycle.send("owner_stopped", "owner_action", "test")

    assert lifecycle.state["phase"] == "leaving"
    assert await _persisted_evidence(saver) == _evidence(lifecycle)
    kinds = [entry["event"] for entry in lifecycle.state["evidence"]]
    assert kinds.count("admitted") == 1


async def test_send_retry_of_a_genuinely_invalid_event_raises() -> None:
    saver = FlakySaver()
    lifecycle = await _admit_lifecycle(saver)

    with pytest.raises(ValueError, match="invalid lifecycle event"):
        await lifecycle.send("join_requested", "owner_action", "test")


async def test_send_of_a_genuinely_invalid_event_does_not_wedge_later_sends() -> None:
    saver = FlakySaver()
    lifecycle = await _admit_lifecycle(saver)

    with pytest.raises(ValueError, match="invalid lifecycle event"):
        await lifecycle.send("join_requested", "owner_action", "test")

    await lifecycle.send("owner_stopped", "owner_action", "test")

    assert lifecycle.state["phase"] == "leaving"
    assert await _persisted_evidence(saver) == _evidence(lifecycle)


def _lifecycle_event(
    kind: LifecycleEventKind, attribution: LifecycleAttribution, event_id: str
) -> LifecycleEvent:
    return {"kind": kind, "attribution": attribution, "detail": "test", "event_id": event_id}


def test_fold_skips_events_invalid_for_the_folded_phase() -> None:
    admitted = fold_lifecycle_events(
        initial_lifecycle_state("owner-1", "twin-1", "meeting-1"),
        [
            _lifecycle_event("join_requested", "owner_action", "join"),
            _lifecycle_event("admitted", "host_action", "admit"),
        ],
    )

    folded = fold_lifecycle_events(
        admitted,
        [
            _lifecycle_event("admitted", "host_action", "poison"),
            _lifecycle_event("owner_stopped", "owner_action", "stop"),
        ],
    )

    assert admitted["phase"] == "admitted"
    assert folded["phase"] == "leaving"
    assert [entry["event"] for entry in folded["evidence"]] == [
        "join_requested",
        "admitted",
        "owner_stopped",
    ]
    assert folded["external_effects"] == ["join_requested", "leave_requested"]


async def test_send_recovery_adopts_commit_lost_during_outage() -> None:
    saver = FlakySaver()
    lifecycle = await _admit_lifecycle(saver)
    saver.mode = "raise"

    await lifecycle.send("owner_stopped", "owner_action", "test")
    saver.mode = "commit_then_raise"
    with pytest.raises(FakeYouError, match="unreachable"):
        await lifecycle.send("leave_sent", "owner_action", "test")
    saver.mode = "ok"

    await lifecycle.send("leave_sent", "owner_action", "test")

    assert lifecycle.state["phase"] == "ended"
    assert await _persisted_evidence(saver) == _evidence(lifecycle)
    kinds = [entry["event"] for entry in lifecycle.state["evidence"]]
    assert kinds.count("owner_stopped") == 1
    assert kinds.count("leave_sent") == 1


async def test_send_failure_while_admitted_degrades_and_replays_on_next_send() -> None:
    saver = FlakySaver()
    lifecycle, journal = _observed_lifecycle(saver)
    await lifecycle.initialize()
    await lifecycle.send("join_requested", "owner_action", "test")
    await lifecycle.send("admitted", "host_action", "test")
    saver.mode = "raise"

    await lifecycle.send("owner_stopped", "owner_action", "test")

    assert lifecycle.state["phase"] == "leaving"
    [queued] = journal.events("lifecycle_persist_queued")
    assert queued["kind"] == "owner_stopped"
    assert queued["error_type"] == "OperationalError"
    assert "connection reset" not in queued.get("detail", "")
    assert journal.events("fault_recorded") != []
    saver.mode = "ok"

    await lifecycle.send("leave_sent", "owner_action", "test")

    ended_phase: str = lifecycle.state["phase"]
    assert ended_phase == "ended"
    saved = await saver.aget_tuple(
        {"configurable": {"thread_id": meeting_thread_id("twin-1", "meeting-1")}}
    )
    assert saved is not None
    assert [entry["event"] for entry in saved.checkpoint["channel_values"]["evidence"]] == [
        "join_requested",
        "admitted",
        "owner_stopped",
        "leave_sent",
    ]


class _ExpiredTimeout:
    async def __aenter__(self) -> object:
        return self

    async def __aexit__(self, *args: Any) -> None:
        raise TimeoutError("expired at exit")


async def test_send_timeout_after_successful_persist_before_admitted_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver = FlakySaver()
    lifecycle, journal = _observed_lifecycle(saver)
    await lifecycle.initialize()
    monkeypatch.setattr(asyncio, "timeout", lambda _seconds: _ExpiredTimeout())

    await lifecycle.send("join_requested", "owner_action", "test")

    assert await _persisted_evidence(saver) == _evidence(lifecycle)
    assert journal.events("lifecycle_persist_queued") == []


async def test_send_timeout_after_successful_persist_while_admitted_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver = FlakySaver()
    lifecycle, journal = _observed_lifecycle(saver)
    await lifecycle.initialize()
    await lifecycle.send("join_requested", "owner_action", "test")
    monkeypatch.setattr(asyncio, "timeout", lambda _seconds: _ExpiredTimeout())

    await lifecycle.send("admitted", "host_action", "test")

    assert lifecycle.state["phase"] == "admitted"
    assert await _persisted_evidence(saver) == _evidence(lifecycle)
    assert journal.events("lifecycle_persist_queued") == []
