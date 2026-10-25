import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast, get_args, override

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import RetryPolicy
from psycopg import OperationalError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.state import (
    LifecycleState,
    MissingRuntimeHandle,
    PublicPhase,
    RuntimeContext,
    assert_checkpointable,
    validate_phase_reason,
)
from fakeyou.conversation.turn_graph import ConversationPhase
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.foundation.taxonomy import MeetingPhase
from fakeyou.meet.meet import JoinResult, JoinStatus
from fakeyou.observability.faults import FaultReporter
from fakeyou.orchestration.lifecycle import (
    _TRANSITIONS,
    CheckpointDegradation,
    DegradingCheckpointer,
    JoinAttempt,
    JoinRetryRequested,
    JoinStep,
    LifecycleAttribution,
    LifecycleEventKind,
    LifecycleThread,
    LifecycleUpdate,
    apply_transition,
    build_lifecycle_graph,
    initial_lifecycle_state,
    initialize_lifecycle,
    join_retry_policy,
    meeting_thread_id,
    resume_lifecycle,
)
from tests._persistence_fakes import RecordingJournal

PUBLIC_PHASES = frozenset(
    {"created", "joining", "lobby", "admitted", "active", "leaving", "ended", "failed"}
)
PUBLIC_TERMINAL_PHASES = frozenset({"ended", "failed"})
LEAK_MARKERS = frozenset({"ya", "sub-state"})
PHASE_MAP: tuple[tuple[str, str, str, str, str], ...] = (
    ("lobby", "lifecycle", "lobby", "—", "ya"),
    ("admitted", "lifecycle", "admitted", "—", "ya"),
    ("disconnected", "lifecycle", "joining", "—", "ya"),
    ("leaving", "lifecycle", "leaving", "—", "ya"),
    ("ended", "lifecycle", "ended", "per transisi", "ya"),
    ("listening", "conversation", "active", "—", "sub-state"),
    ("weighing", "conversation", "active", "—", "sub-state"),
    ("queued", "conversation", "active", "—", "sub-state"),
    ("interrupted", "conversation", "active", "—", "sub-state"),
    ("waiting", "conversation", "active", "—", "sub-state"),
    ("acting", "conversation", "active", "—", "sub-state"),
    ("failed", "conversation", "failed", "conversation_failure", "ya"),
    ("closed", "conversation", "leaving", "—", "sub-state"),
)
THREAD = meeting_thread_id("twin-1", "meeting-1")
JOIN_MAX_ATTEMPTS = 3
JOIN_RETRY_BASE_SECONDS = 2.0
JOIN_RETRY_CAP_SECONDS = 30.0
PINNED_JITTER = 0.25
FAST_JOIN_RETRY = join_retry_policy(
    max_attempts=JOIN_MAX_ATTEMPTS, base_seconds=0.001, cap_seconds=0.001
)
_SCRIPTED_JOIN_EVENTS: dict[JoinStatus, tuple[LifecycleEventKind, LifecycleAttribution]] = {
    "denied": ("denied", "host_action"),
    "blocked": ("blocked", "system_fault"),
    "timeout": ("lobby_timed_out", "system_fault"),
    "aborted": ("owner_stopped", "owner_action"),
    "failed": ("browser_failed", "system_fault"),
}


def scripted_join_step(state: LifecycleState, result: JoinResult) -> JoinStep:
    requested = apply_transition(state, "join_requested", "owner_action")
    if result.status == "admitted":
        return JoinStep(result, requested)
    kind, attribution = _SCRIPTED_JOIN_EVENTS[result.status]
    settled = apply_transition(cast(LifecycleState, {**state, **requested}), kind, attribution)
    return JoinStep(result, {**requested, **settled})


class _RecordingDriver:
    def __init__(self) -> None:
        self.journal = RecordingJournal()

    def record(self, event: str, **fields: Any) -> None:
        self.journal.record(event, **fields)

    async def prepare(self, state: LifecycleState) -> LifecycleUpdate:
        del state
        return {"phase": "joining"}

    async def join(self, state: LifecycleState, attempt: JoinAttempt) -> JoinStep:
        del attempt
        return scripted_join_step(state, JoinResult(status="admitted", detail="admitted"))

    async def await_admission(self, state: LifecycleState) -> LifecycleUpdate:
        del state
        return {"phase": "admitted"}

    async def activate(self, state: LifecycleState) -> LifecycleUpdate:
        del state
        return {"phase": "active"}

    async def monitor(self, state: LifecycleState) -> LifecycleUpdate:
        return {
            "phase": "leaving",
            "terminal_reason": "stopped_by_owner",
            "external_effects": [*state["external_effects"], {"effect": "leave_requested"}],
        }

    async def leave(self, state: LifecycleState) -> LifecycleUpdate:
        del state
        return {}


class FlakySaver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.mode = "ok"
        self.released = __import__("asyncio").Event()

    @override
    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        if self.mode == "raise":
            raise OperationalError("connection reset by peer")
        if self.mode == "hang":
            await self.released.wait()
        result = await super().aput(config, checkpoint, metadata, new_versions)
        if self.mode == "commit_then_raise":
            raise OperationalError("response lost after commit")
        return result


class UnserializableSaver(InMemorySaver):
    @override
    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        raise TypeError("Type is not msgpack serializable: async_generator")


class _ExpiredTimeout:
    async def __aenter__(self) -> object:
        return self

    async def __aexit__(self, *args: Any) -> None:
        raise TimeoutError("expired at exit")


def initial_state(owner_id: str, twin_id: str, meeting_id: str) -> LifecycleState:
    return initial_lifecycle_state(owner_id, twin_id, meeting_id)


def full_runtime_context() -> RuntimeContext:
    return RuntimeContext(browser=object(), session=_RecordingDriver())


def _in_phase(phase: PublicPhase, **fields: Any) -> LifecycleState:
    return cast(LifecycleState, {**initial_state("o1", "t1", "m1"), "phase": phase, **fields})


def _apply(
    state: LifecycleState,
    kind: LifecycleEventKind,
    attribution: LifecycleAttribution = "owner_action",
    previous: PublicPhase | None = None,
) -> LifecycleUpdate:
    return apply_transition(state, kind, attribution, previous=previous)


def _advance(state: LifecycleState, update: LifecycleUpdate) -> LifecycleState:
    return cast(LifecycleState, {**state, **update})


async def _thread(
    saver: Any,
    journal: RecordingJournal | None = None,
    *,
    owner: str = "owner-1",
    send_timeout: float = 5.0,
) -> LifecycleThread:
    faults = FaultReporter(journal=journal) if journal is not None else None
    policy = CheckpointDegradation(
        send_timeout_seconds=send_timeout, faults=faults, journal=journal
    )
    graph = build_lifecycle_graph(DegradingCheckpointer(saver, policy))
    thread = LifecycleThread(graph, THREAD, initial_lifecycle_state(owner, "twin-1", "meeting-1"))
    await initialize_lifecycle(thread, resume=False)
    return thread


async def _durable(saver: Any) -> dict[str, Any]:
    saved = await saver.aget_tuple({"configurable": {"thread_id": THREAD}})
    assert saved is not None
    return cast(dict[str, Any], saved.checkpoint["channel_values"])


async def _admitted_thread(saver: Any, journal: RecordingJournal | None = None) -> LifecycleThread:
    thread = await _thread(saver, journal)
    await thread.send("prepared", "owner_action")
    await thread.send("join_requested", "owner_action")
    await thread.send("admitted", "host_action")
    assert (await thread.read())["phase"] == "admitted"
    return thread


async def checkpointed_active_session() -> tuple[
    CompiledStateGraph[Any, Any, Any, Any], RunnableConfig
]:
    graph = build_lifecycle_graph(checkpointer=InMemorySaver())
    active_cfg: RunnableConfig = {"configurable": {"thread_id": "t1:m1"}}
    await graph.aupdate_state(
        active_cfg,
        {
            **initial_state("o1", "t1", "m1"),
            "phase": "active",
            "external_effects": [{"effect": "join_requested"}],
        },
        as_node="activate",
    )
    done_cfg: RunnableConfig = {"configurable": {"thread_id": "t1:done"}}
    await graph.aupdate_state(
        done_cfg,
        {**initial_state("o1", "t1", "done"), "phase": "ended", "terminal_reason": "left_by_owner"},
        as_node="leave",
    )
    return graph, active_cfg


@pytest.mark.asyncio
async def test_lifecycle_graph_walks_public_phases_in_order() -> None:
    graph = build_lifecycle_graph(checkpointer=InMemorySaver())
    cfg: RunnableConfig = {"configurable": {"thread_id": "t1:m1"}}
    phases = []
    async for chunk in graph.astream(
        initial_state(owner_id="o1", twin_id="t1", meeting_id="m1"),
        config=cfg,
        context=full_runtime_context(),
        stream_mode="values",
    ):
        phases.append(chunk["phase"])
    assert phases == [
        "created",
        "joining",
        "lobby",
        "admitted",
        "active",
        "leaving",
        "ended",
    ]


@pytest.mark.asyncio
async def test_missing_handle_after_restart_fails_with_reason() -> None:
    graph = build_lifecycle_graph(checkpointer=InMemorySaver())
    cfg: RunnableConfig = {"configurable": {"thread_id": "t1:m1"}}
    out = await graph.ainvoke(
        initial_state(owner_id="o1", twin_id="t1", meeting_id="m1"),
        config=cfg,
        context=RuntimeContext(),
    )
    assert out["phase"] == "ended"
    assert out["terminal_reason"].startswith("runtime_handle_missing:")


@pytest.mark.asyncio
async def test_resume_is_explicit_and_keeps_the_effects() -> None:
    graph, _cfg = await checkpointed_active_session()
    resumed = await resume_lifecycle(graph, "t1:m1", full_runtime_context())
    assert resumed["thread_id"] == "t1:m1"
    assert resumed["external_effects"] != []
    with pytest.raises(ValueError, match="ended"):
        await resume_lifecycle(graph, "t1:done", full_runtime_context())


def test_lifecycle_join_and_leave_happy_path() -> None:
    state = _in_phase("joining")
    state = _advance(state, _apply(state, "join_requested"))
    assert state["phase"] == "lobby"
    state = _advance(state, _apply(state, "admitted", "host_action"))
    assert state["phase"] == "admitted"
    state = _advance(state, _apply(state, "activated", "policy"))
    assert state["phase"] == "active"
    state = _advance(state, _apply(state, "owner_stopped"))
    assert state["phase"] == "leaving"
    state = _advance(state, _apply(state, "leave_sent"))

    assert state["phase"] == "ended"
    assert state["terminal_reason"] == "stopped_by_owner"
    assert state["external_effects"] == [
        {"effect": "join_requested"},
        {"effect": "leave_requested"},
    ]


async def test_lifecycle_resume_restarts_a_crashed_thread_in_place() -> None:
    saver = FlakySaver()
    first = await _admitted_thread(saver)
    assert (await first.read())["external_effects"] == [{"effect": "join_requested"}]

    resumed = LifecycleThread(first.graph, THREAD, first.initial)
    with pytest.raises(FakeYouError, match="cannot resume meeting") as caught:
        await initialize_lifecycle(resumed, resume=False)
    assert caught.value.code is ErrorCode.MEETING_CANNOT_RESUME_PHASE

    saved = await initialize_lifecycle(resumed, resume=True)

    assert saved["phase"] == "admitted"
    assert saved["external_effects"] == [{"effect": "join_requested"}]
    durable = await _durable(saver)
    assert durable["phase"] == "admitted"
    assert durable["external_effects"] == saved["external_effects"]

    finished = await resume_lifecycle(first.graph, THREAD, full_runtime_context())
    assert finished["phase"] == "ended"
    assert finished["external_effects"] == [
        {"effect": "join_requested"},
        {"effect": "leave_requested"},
    ]


async def test_lifecycle_resume_still_rejects_an_identity_mismatch() -> None:
    saver = InMemorySaver()
    first = await _thread(saver)
    await first.send("prepared", "owner_action")
    await first.send("join_requested", "owner_action")

    other = LifecycleThread(
        first.graph, THREAD, initial_lifecycle_state("owner-2", "twin-1", "meeting-1")
    )
    with pytest.raises(FakeYouError, match="checkpoint identity does not match") as caught:
        await initialize_lifecycle(other, resume=True)
    assert caught.value.code is ErrorCode.MEETING_CHECKPOINT_IDENTITY_MISMATCH


@pytest.mark.parametrize(
    ("kind", "attribution", "reason"),
    [
        ("denied", "host_action", "denied_by_host"),
        ("blocked", "system_fault", "blocked_by_platform"),
        ("lobby_timed_out", "system_fault", "lobby_timeout_reached"),
    ],
)
def test_lobby_outcomes_end_the_session(
    kind: LifecycleEventKind, attribution: LifecycleAttribution, reason: str
) -> None:
    update = _apply(_in_phase("lobby"), kind, attribution)

    assert update["phase"] == "ended"
    assert update["terminal_reason"] == reason


async def test_lifecycle_denied_by_host() -> None:
    thread = await _thread(FlakySaver())
    await thread.send("prepared", "owner_action")
    await thread.send("join_requested", "owner_action")

    await thread.send("denied", "host_action")

    state = await thread.read()
    assert state["phase"] == "ended"
    assert state["terminal_reason"] == "denied_by_host"


async def test_lifecycle_platform_block_ends_session() -> None:
    thread = await _thread(FlakySaver())
    await thread.send("prepared", "owner_action")
    await thread.send("join_requested", "owner_action")

    await thread.send("blocked", "system_fault")

    state = await thread.read()
    assert state["phase"] == "ended"
    assert state["terminal_reason"] == "blocked_by_platform"


async def test_lifecycle_lobby_timeout() -> None:
    thread = await _thread(FlakySaver())
    await thread.send("prepared", "owner_action")
    await thread.send("join_requested", "owner_action")

    await thread.send("lobby_timed_out", "system_fault")

    state = await thread.read()
    assert state["phase"] == "ended"
    assert state["terminal_reason"] == "lobby_timeout_reached"


async def test_lifecycle_removed_by_host() -> None:
    thread = await _admitted_thread(FlakySaver())

    await thread.send("removed", "host_action")

    state = await thread.read()
    assert state["phase"] == "ended"
    assert state["terminal_reason"] == "removed_by_host"


async def test_lifecycle_reconnect_returns_to_previous_phase() -> None:
    thread = await _admitted_thread(FlakySaver())
    await thread.send("connection_lost", "system_fault")
    dropped = await thread.read()
    assert dropped["phase"] == "joining"

    restored = _apply(dropped, "reconnected", "system_fault", previous="admitted")

    assert restored == {"phase": "admitted"}


async def test_lifecycle_duration_limit_leaves_with_reason() -> None:
    thread = await _admitted_thread(FlakySaver())

    await thread.send("duration_reached", "policy")
    leaving = await thread.read()
    assert leaving["phase"] == "leaving"
    assert leaving["terminal_reason"] == "duration_limit_reached"

    await thread.send("leave_sent", "policy")
    result = await thread.read()
    assert result["phase"] == "ended"
    assert result["terminal_reason"] == "duration_limit_reached"


async def test_lifecycle_duration_limit_ends_disconnected_session() -> None:
    thread = await _admitted_thread(FlakySaver())
    await thread.send("connection_lost", "system_fault")

    await thread.send("duration_reached", "policy")

    state = await thread.read()
    assert state["phase"] == "ended"
    assert state["terminal_reason"] == "duration_limit_reached"


async def test_lifecycle_ended_ignores_further_events() -> None:
    saver = FlakySaver()
    thread = await _thread(saver)
    await thread.send("prepared", "owner_action")
    await thread.send("owner_stopped", "owner_action")
    before = await _durable(saver)

    await thread.send("admitted", "host_action")

    after = await _durable(saver)
    assert after["phase"] == "ended"
    assert after["terminal_reason"] == "stopped_by_owner"
    assert after["external_effects"] == before["external_effects"]


def test_lifecycle_rejects_invalid_event_for_phase() -> None:
    with pytest.raises(ValueError, match="invalid lifecycle event"):
        _apply(_in_phase("created"), "admitted", "host_action")


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("audio_failed", "audio_failure"), ("conversation_failed", "conversation_failure")],
)
@pytest.mark.parametrize(
    "drive",
    [
        [],
        ["prepared", "join_requested"],
        ["prepared", "join_requested", ("admitted", "host_action"), "owner_stopped"],
    ],
    ids=["preparing", "lobby", "leaving"],
)
async def test_startup_and_teardown_failures_end_the_session(
    kind: str, reason: str, drive: list[str | tuple[str, str]]
) -> None:
    thread = await _thread(FlakySaver())
    for step in drive:
        if isinstance(step, tuple):
            await thread.send(
                cast(LifecycleEventKind, step[0]), cast(LifecycleAttribution, step[1])
            )
        else:
            await thread.send(cast(LifecycleEventKind, step), "owner_action")

    await thread.send(cast(LifecycleEventKind, kind), "system_fault")

    state = await thread.read()
    expected_phase = "failed" if reason == "conversation_failure" else "ended"
    assert state["phase"] == expected_phase
    assert state["terminal_reason"] == reason


def test_lifecycle_rejects_unknown_attribution() -> None:
    with pytest.raises(ValueError, match="unknown lifecycle attribution"):
        _apply(_in_phase("created"), "prepared", cast(LifecycleAttribution, "mystery"))


async def test_send_maps_a_hung_checkpoint_write_to_unreachable() -> None:
    saver = FlakySaver()
    thread = await _thread(saver, send_timeout=0.05)
    saver.mode = "hang"

    try:
        with pytest.raises(FakeYouError, match="unreachable") as caught:
            await thread.send("prepared", "owner_action")
    finally:
        saver.released.set()

    assert caught.value.code is ErrorCode.DATABASE_UNREACHABLE


async def test_a_failed_checkpoint_write_never_breaks_the_meeting() -> None:
    saver = UnserializableSaver()
    journal = RecordingJournal("meeting-1")
    checkpointer = DegradingCheckpointer(saver, CheckpointDegradation(faults=None, journal=journal))

    await checkpointer.aput_writes(
        {"configurable": {"thread_id": THREAD}}, [("reply", object())], "task-1"
    )

    [failed] = journal.events("checkpoint_write_failed")
    assert failed["error_type"] == "TypeError"
    assert failed["culprit"] == "write['reply']: object"
    assert failed["channels"] == ["reply"]


async def test_send_failure_before_admitted_raises_unreachable() -> None:
    saver = FlakySaver()
    thread = await _thread(saver)
    await thread.send("prepared", "owner_action")
    saver.mode = "raise"

    with pytest.raises(FakeYouError, match="unreachable") as caught:
        await thread.send("join_requested", "owner_action")

    assert caught.value.code is ErrorCode.DATABASE_UNREACHABLE
    assert (await thread.read())["phase"] == "joining"


async def test_send_failure_while_admitted_degrades_and_replays_on_next_send() -> None:
    saver = FlakySaver()
    journal = RecordingJournal("meeting-1")
    thread = await _admitted_thread(saver, journal)
    saver.mode = "raise"

    await thread.send("owner_stopped", "owner_action")

    assert (await thread.read())["phase"] == "leaving"
    [queued] = journal.events("lifecycle_persist_queued")
    assert queued["kind"] == "owner_stopped"
    assert queued["error_type"] == "OperationalError"
    assert "connection reset" not in queued.get("detail", "")
    assert journal.events("fault_recorded") != []
    assert (await _durable(saver))["phase"] == "admitted"
    saver.mode = "ok"

    await thread.send("leave_sent", "owner_action")

    assert (await thread.read())["phase"] == "ended"
    durable = await _durable(saver)
    assert durable["phase"] == "ended"
    assert durable["terminal_reason"] == "stopped_by_owner"
    assert durable["external_effects"] == [
        {"effect": "join_requested"},
        {"effect": "leave_requested"},
    ]


async def test_send_recovery_adopts_commit_lost_during_outage() -> None:
    saver = FlakySaver()
    thread = await _admitted_thread(saver)
    saver.mode = "raise"

    await thread.send("owner_stopped", "owner_action")
    saver.mode = "commit_then_raise"
    await thread.send("leave_sent", "owner_action")

    assert (await thread.read())["phase"] == "ended"
    durable = await _durable(saver)
    assert durable["phase"] == "ended"
    assert durable["external_effects"] == [
        {"effect": "join_requested"},
        {"effect": "leave_requested"},
    ]


async def test_send_retry_adopts_an_already_applied_event() -> None:
    saver = FlakySaver()
    thread = await _admitted_thread(saver)
    saver.mode = "commit_then_raise"

    await thread.send("owner_stopped", "owner_action")
    saver.mode = "ok"
    await thread.send("leave_sent", "owner_action")

    state = await thread.read()
    assert state["phase"] == "ended"
    assert state["terminal_reason"] == "stopped_by_owner"
    durable = await _durable(saver)
    assert durable["phase"] == state["phase"]
    assert durable["external_effects"].count({"effect": "leave_requested"}) == 1


async def test_send_retry_drops_an_event_with_no_forward_transition() -> None:
    saver = FlakySaver()
    thread = await _thread(saver)
    await thread.send("prepared", "owner_action")
    await thread.send("join_requested", "owner_action")
    saver.mode = "commit_then_raise"

    await thread.send("admitted", "host_action")
    saver.mode = "ok"
    await thread.send("owner_stopped", "owner_action")

    assert (await thread.read())["phase"] == "leaving"
    assert (await _durable(saver))["phase"] == "leaving"


async def test_send_retry_of_a_genuinely_invalid_event_raises() -> None:
    thread = await _admitted_thread(FlakySaver())

    with pytest.raises(ValueError, match="invalid lifecycle event"):
        await thread.send("join_requested", "owner_action")


async def test_send_of_a_genuinely_invalid_event_does_not_wedge_later_sends() -> None:
    saver = FlakySaver()
    thread = await _admitted_thread(saver)

    with pytest.raises(ValueError, match="invalid lifecycle event"):
        await thread.send("join_requested", "owner_action")

    await thread.send("owner_stopped", "owner_action")

    assert (await thread.read())["phase"] == "leaving"
    assert (await _durable(saver))["phase"] == "leaving"


async def test_fold_skips_events_invalid_for_the_folded_phase() -> None:
    saver = InMemorySaver()
    thread = await _thread(saver)
    await thread.send("prepared", "owner_action")
    await thread.send("join_requested", "owner_action")
    before = await _durable(saver)

    with pytest.raises(ValueError, match="invalid lifecycle event"):
        await thread.send("activated", "policy")

    assert (await thread.read())["phase"] == "lobby"
    assert await _durable(saver) == before
    await thread.send("denied", "host_action")
    assert (await _durable(saver))["phase"] == "ended"


async def test_send_timeout_after_successful_persist_before_admitted_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver = FlakySaver()
    journal = RecordingJournal("meeting-1")
    thread = await _thread(saver, journal)
    await thread.send("prepared", "owner_action")
    monkeypatch.setattr(asyncio_module(), "timeout", lambda _seconds: _ExpiredTimeout())

    await thread.send("join_requested", "owner_action")

    assert (await _durable(saver))["phase"] == "lobby"
    assert journal.events("lifecycle_persist_queued") == []


async def test_send_timeout_after_successful_persist_while_admitted_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver = FlakySaver()
    journal = RecordingJournal("meeting-1")
    thread = await _thread(saver, journal)
    await thread.send("prepared", "owner_action")
    await thread.send("join_requested", "owner_action")
    monkeypatch.setattr(asyncio_module(), "timeout", lambda _seconds: _ExpiredTimeout())

    await thread.send("admitted", "host_action")

    assert (await thread.read())["phase"] == "admitted"
    assert (await _durable(saver))["phase"] == "admitted"
    assert journal.events("lifecycle_persist_queued") == []


def asyncio_module() -> Any:
    import asyncio

    return asyncio


def _phase_map_rows() -> list[list[str]]:
    return [list(row) for row in PHASE_MAP]


def test_phase_map_covers_every_phase_once() -> None:
    mapped = [row[0] for row in _phase_map_rows()]
    expected = set(get_args(ConversationPhase)) | set(get_args(MeetingPhase))

    assert {phase for phase, _event in _TRANSITIONS} <= PUBLIC_PHASES
    assert set(mapped) == expected
    assert len(mapped) == len(set(mapped))


def test_phase_map_uses_only_frozen_public_phases() -> None:
    for _old, _source, public, reason, leak in _phase_map_rows():
        assert public in PUBLIC_PHASES
        assert leak in LEAK_MARKERS
        if public in PUBLIC_TERMINAL_PHASES:
            assert reason != "—"
        else:
            assert reason == "—"


def test_phase_map_sources_match_the_enums() -> None:
    sources = {old: source for old, source, *_ in _phase_map_rows()}

    assert {sources[phase] for phase in get_args(ConversationPhase)} == {"conversation"}
    assert {sources[phase] for phase in get_args(MeetingPhase)} == {"lifecycle"}


def test_checkpoint_state_rejects_runtime_handles() -> None:
    state = {
        "owner_id": "o1",
        "twin_id": "t1",
        "meeting_id": "m1",
        "thread_id": "t1:m1",
        "phase": "created",
        "terminal_reason": None,
        "external_effects": [],
        "browser": object(),
    }
    with pytest.raises(ValueError, match="browser"):
        assert_checkpointable(cast(LifecycleState, state))


def test_terminal_phase_requires_reason() -> None:
    with pytest.raises(ValueError, match="terminal_reason"):
        validate_phase_reason("failed", None)
    validate_phase_reason("failed", "join_failed")
    validate_phase_reason("active", None)


def test_missing_runtime_handle_is_named() -> None:
    with pytest.raises(MissingRuntimeHandle, match="browser"):
        RuntimeContext().require("browser")


@pytest.mark.asyncio
async def test_lifecycle_state_survives_postgres_reopen(migrated_database_url: str) -> None:
    config: RunnableConfig = {"configurable": {"thread_id": "twin-1:meeting-1"}}
    async with open_checkpointer(migrated_database_url) as saver:
        first = build_lifecycle_graph(checkpointer=saver)
        await first.aupdate_state(
            config,
            {
                **initial_state("owner-1", "twin-1", "meeting-1"),
                "phase": "admitted",
                "external_effects": [{"effect": "join_requested"}],
            },
            as_node="await_admission",
        )

    async with open_checkpointer(migrated_database_url) as saver:
        second = build_lifecycle_graph(checkpointer=saver)
        reopened = await second.aget_state(config)

        assert reopened.values["phase"] == "admitted"
        result = await resume_lifecycle(second, "twin-1:meeting-1", full_runtime_context())

    assert result["phase"] == "ended"
    assert result["terminal_reason"] == "stopped_by_owner"
    assert result["external_effects"] == [
        {"effect": "join_requested"},
        {"effect": "leave_requested"},
    ]


def test_meeting_thread_id_is_deterministic() -> None:
    assert meeting_thread_id("twin-1", "meeting-1") == "twin-1:meeting-1"


async def test_initialize_refuses_a_live_thread_without_explicit_resume() -> None:
    graph = build_lifecycle_graph(checkpointer=InMemorySaver())
    initial = initial_state("owner-1", "twin-1", "meeting-1")
    thread = LifecycleThread(graph, "twin-1:meeting-1", initial)
    await graph.aupdate_state(thread.config, {**initial, "phase": "active"}, as_node="activate")

    with pytest.raises(FakeYouError, match="cannot resume meeting") as caught:
        await initialize_lifecycle(thread, resume=False)
    assert caught.value.code is ErrorCode.MEETING_CANNOT_RESUME_PHASE

    saved = await initialize_lifecycle(thread, resume=True)
    assert saved["phase"] == "active"


class FlakyJoiner:
    def __init__(self, failures_before_success: int, *, failure: JoinStatus = "failed") -> None:
        self.failures_before_success = failures_before_success
        self.failure = failure
        self.attempts = 0
        self.seen: list[JoinAttempt] = []
        self.journal = RecordingJournal()

    def record(self, event: str, **fields: Any) -> None:
        self.journal.record(event, **fields)

    def status_for(self, attempt: int) -> JoinStatus:
        return "admitted" if attempt > self.failures_before_success else self.failure

    async def join(self, state: LifecycleState, attempt: JoinAttempt) -> JoinStep:
        self.attempts += 1
        self.seen.append(attempt)
        status = self.status_for(self.attempts)
        return scripted_join_step(state, JoinResult(status=status, detail=f"{status} detail"))

    def outcomes(self) -> list[tuple[int, str]]:
        return [
            (row["attempt"], row["outcome"]) for row in self.journal.events("join_attempt_finished")
        ]


class AbortingJoiner(FlakyJoiner):
    def __init__(self) -> None:
        super().__init__(failures_before_success=99, failure="aborted")


def cfg() -> RunnableConfig:
    return {"configurable": {"thread_id": "t1:m1"}}


@dataclass
class JitterPin:
    value: float = 0.0
    draws: list[tuple[float, float]] = field(default_factory=list)


@pytest.fixture
def pinned_join_jitter(monkeypatch: pytest.MonkeyPatch) -> JitterPin:
    pin = JitterPin()

    def uniform(low: float, high: float) -> float:
        pin.draws.append((low, high))
        return pin.value

    monkeypatch.setattr("langgraph.pregel._retry.random", SimpleNamespace(uniform=uniform))
    return pin


@pytest.fixture
def retry_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    original = asyncio.sleep

    async def record(delay: float, result: Any = None) -> Any:
        if delay > 0:
            slept.append(delay)
        return await original(0, result)

    monkeypatch.setattr(asyncio, "sleep", record)
    return slept


async def _run_join(joiner: FlakyJoiner, policy: RetryPolicy = FAST_JOIN_RETRY) -> LifecycleState:
    graph = build_lifecycle_graph(checkpointer=InMemorySaver(), joiner=joiner, join_retry=policy)
    out: object = await graph.ainvoke(
        initial_state(owner_id="o1", twin_id="t1", meeting_id="m1"),
        config=cfg(),
        context=full_runtime_context(),
    )
    return cast(LifecycleState, out)


def test_join_retry_policy_freezes_the_retired_loop_numbers() -> None:
    policy = join_retry_policy(
        max_attempts=DEFAULTS.meeting.join_max_attempts,
        base_seconds=DEFAULTS.meeting.join_retry_base_seconds,
        cap_seconds=DEFAULTS.meeting.join_retry_cap_seconds,
    )

    assert policy == RetryPolicy(
        initial_interval=JOIN_RETRY_BASE_SECONDS,
        backoff_factor=2.0,
        max_interval=JOIN_RETRY_CAP_SECONDS,
        max_attempts=JOIN_MAX_ATTEMPTS,
        jitter=True,
        retry_on=JoinRetryRequested,
    )
    assert build_lifecycle_graph(InMemorySaver()).nodes["join"].retry_policy == (policy,)


async def test_join_retries_transient_errors_then_succeeds(
    pinned_join_jitter: JitterPin,
) -> None:
    joiner = FlakyJoiner(failures_before_success=2)

    out = await _run_join(joiner)

    assert joiner.attempts == 3
    assert out["phase"] == "ended"
    assert [(attempt.number, attempt.last) for attempt in joiner.seen] == [
        (1, False),
        (2, False),
        (3, True),
    ]
    assert joiner.outcomes() == [(1, "retry"), (2, "retry"), (3, "joined")]
    assert {row["source"] for row in joiner.journal.events("join_attempt_finished")} == {"runtime"}
    assert pinned_join_jitter.draws == [(0, 1), (0, 1)]


@pytest.mark.parametrize(
    ("failure", "reason"),
    [("failed", "browser_failure"), ("timeout", "lobby_timeout_reached")],
)
async def test_join_retry_exhaustion_fails_with_reason(
    pinned_join_jitter: JitterPin, failure: JoinStatus, reason: str
) -> None:
    joiner = FlakyJoiner(failures_before_success=99, failure=failure)

    out = await _run_join(joiner)

    assert joiner.attempts == JOIN_MAX_ATTEMPTS
    assert out["phase"] == "ended"
    assert out["terminal_reason"] == reason
    assert joiner.outcomes() == [(1, "retry"), (2, "retry"), (3, "failed")]
    assert len(pinned_join_jitter.draws) == JOIN_MAX_ATTEMPTS - 1


async def test_owner_abort_is_not_retried(pinned_join_jitter: JitterPin) -> None:
    joiner = AbortingJoiner()

    out = await _run_join(joiner)

    assert joiner.attempts == 1
    assert out["phase"] == "ended"
    assert out["terminal_reason"] == "stopped_by_owner"
    assert joiner.outcomes() == [(1, "aborted")]
    assert pinned_join_jitter.draws == []


@pytest.mark.parametrize(
    ("failure", "reason"),
    [("denied", "denied_by_host"), ("blocked", "blocked_by_platform")],
)
async def test_domain_join_outcomes_end_on_the_first_attempt(
    pinned_join_jitter: JitterPin, failure: JoinStatus, reason: str
) -> None:
    joiner = FlakyJoiner(failures_before_success=99, failure=failure)

    out = await _run_join(joiner)

    assert joiner.attempts == 1
    assert out["terminal_reason"] == reason
    assert joiner.outcomes() == [(1, "failed")]
    assert pinned_join_jitter.draws == []


async def test_join_backoff_sleeps_the_capped_interval_plus_jitter(
    pinned_join_jitter: JitterPin, retry_sleeps: list[float]
) -> None:
    pinned_join_jitter.value = PINNED_JITTER
    joiner = FlakyJoiner(failures_before_success=3, failure="timeout")
    policy = join_retry_policy(max_attempts=4, base_seconds=2.0, cap_seconds=5.0)

    out = await _run_join(joiner, policy)

    assert out["phase"] == "ended"
    assert joiner.outcomes() == [(1, "retry"), (2, "retry"), (3, "retry"), (4, "joined")]
    assert retry_sleeps == [2.0 + PINNED_JITTER, 4.0 + PINNED_JITTER, 5.0 + PINNED_JITTER]
    assert pinned_join_jitter.draws == [(0, 1), (0, 1), (0, 1)]


async def test_join_without_a_browser_fails_before_any_attempt() -> None:
    joiner = FlakyJoiner(failures_before_success=0)
    graph = build_lifecycle_graph(checkpointer=InMemorySaver(), joiner=joiner)

    out = await graph.ainvoke(
        initial_state(owner_id="o1", twin_id="t1", meeting_id="m1"),
        config=cfg(),
        context=RuntimeContext(session=_RecordingDriver()),
    )

    assert joiner.attempts == 0
    assert out["terminal_reason"] == "runtime_handle_missing:browser"
