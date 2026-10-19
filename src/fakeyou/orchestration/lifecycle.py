import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, NamedTuple, Protocol, TypedDict, cast, get_args, override

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, RetryPolicy

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.state import (
    TERMINAL_PHASES,
    ExternalEffect,
    LifecycleState,
    MissingRuntimeHandle,
    PublicPhase,
    RuntimeContext,
    validate_phase_reason,
)
from fakeyou.db.checkpoint import is_checkpoint_error
from fakeyou.foundation.provenance import MemoryEvidence
from fakeyou.foundation.taxonomy import FaultKind, TerminalReason
from fakeyou.meet.meet import JoinResult, JoinStatus
from fakeyou.observability.faults import Fault, FaultReporter
from fakeyou.observability.journal import Journal

LifecycleAttribution = Literal["host_action", "system_fault", "owner_action", "policy"]
LifecycleEventKind = Literal[
    "prepared",
    "infra_failed",
    "participants_left",
    "join_requested",
    "admitted",
    "activated",
    "denied",
    "blocked",
    "lobby_timed_out",
    "owner_stopped",
    "browser_failed",
    "audio_failed",
    "conversation_failed",
    "connection_lost",
    "reconnected",
    "duration_reached",
    "removed",
    "meeting_ended",
    "leave_sent",
    "cancelled",
]

_ATTRIBUTIONS = frozenset(get_args(LifecycleAttribution))
_LIVE: tuple[PublicPhase, ...] = ("admitted", "active")
_STARTING: tuple[PublicPhase, ...] = ("created", "joining", "lobby")


@dataclass(frozen=True, slots=True)
class _Transition:
    phase: PublicPhase
    reason: TerminalReason | None = None
    effect: str | None = None
    pending: TerminalReason | None = None


def _edges() -> dict[tuple[PublicPhase, str], _Transition]:
    edges: dict[tuple[PublicPhase, str], _Transition] = {}

    def add(phases: tuple[PublicPhase, ...], kind: str, transition: _Transition) -> None:
        for phase in phases:
            edges[(phase, kind)] = transition

    add(("created",), "prepared", _Transition("joining"))
    add(("created",), "infra_failed", _Transition("ended", reason="infra_failure"))
    add(_STARTING, "owner_stopped", _Transition("ended", reason="stopped_by_owner"))
    add(_STARTING, "browser_failed", _Transition("ended", reason="browser_failure"))
    add(_STARTING, "audio_failed", _Transition("ended", reason="audio_failure"))
    add(_STARTING, "conversation_failed", _Transition("ended", reason="conversation_failure"))
    add(("joining",), "join_requested", _Transition("lobby", effect="join_requested"))
    add(("lobby",), "admitted", _Transition("admitted"))
    add(("lobby",), "denied", _Transition("ended", reason="denied_by_host"))
    add(("lobby",), "blocked", _Transition("ended", reason="blocked_by_platform"))
    add(("lobby",), "lobby_timed_out", _Transition("ended", reason="lobby_timeout_reached"))
    add(("joining",), "denied", _Transition("ended", reason="denied_by_host"))
    add(("joining",), "lobby_timed_out", _Transition("ended", reason="lobby_timeout_reached"))
    add(("joining",), "participants_left", _Transition("ended", reason="alone_limit_reached"))
    add(("joining",), "meeting_ended", _Transition("ended", reason="ended_by_host"))
    add(("joining",), "removed", _Transition("ended", reason="removed_by_host"))
    add(("joining",), "duration_reached", _Transition("ended", reason="duration_limit_reached"))
    leaving = _Transition("leaving", effect="leave_requested")
    add(_LIVE, "participants_left", replace(leaving, pending="alone_limit_reached"))
    add(_LIVE, "owner_stopped", replace(leaving, pending="stopped_by_owner"))
    add(_LIVE, "duration_reached", replace(leaving, pending="duration_limit_reached"))
    add(_LIVE, "removed", _Transition("ended", reason="removed_by_host"))
    add(_LIVE, "meeting_ended", _Transition("ended", reason="ended_by_host"))
    add(_LIVE, "browser_failed", _Transition("ended", reason="browser_failure"))
    add(_LIVE, "audio_failed", replace(leaving, pending="audio_failure"))
    add(_LIVE, "conversation_failed", replace(leaving, pending="conversation_failure"))
    add(_LIVE, "connection_lost", _Transition("joining"))
    add(("admitted",), "activated", _Transition("active"))
    add(("leaving",), "leave_sent", _Transition("ended", reason="left_by_owner"))
    add(("leaving",), "owner_stopped", _Transition("leaving"))
    add(("leaving",), "browser_failed", _Transition("ended", reason="browser_failure"))
    add(("leaving",), "audio_failed", _Transition("ended", reason="audio_failure"))
    add(("leaving",), "conversation_failed", _Transition("ended", reason="conversation_failure"))
    return edges


_TRANSITIONS: dict[tuple[PublicPhase, str], _Transition] = _edges()


class LifecycleUpdate(TypedDict, total=False):
    phase: PublicPhase
    terminal_reason: str | None
    external_effects: list[ExternalEffect]
    memory_evidence: list[MemoryEvidence]


def meeting_thread_id(twin_id: str, meeting_id: str) -> str:
    return f"{twin_id}:{meeting_id}"


def initial_lifecycle_state(owner_id: str, twin_id: str, meeting_id: str) -> LifecycleState:
    thread_id = meeting_thread_id(twin_id, meeting_id)
    return {
        "owner_id": owner_id,
        "twin_id": twin_id,
        "meeting_id": meeting_id,
        "thread_id": thread_id,
        "phase": "created",
        "terminal_reason": None,
        "messages": [],
        "memory_evidence": [],
        "last_participation": None,
        "note_offset": 0,
        "note_prefix_hash": "",
        "external_effects": [],
    }


def _terminal_phase(reason: str | None) -> PublicPhase:
    return "failed" if reason == "conversation_failure" else "ended"


def _resolve_reason(
    state: LifecycleState, phase: PublicPhase, kind: str, transition: _Transition
) -> TerminalReason | None:
    if phase == "leaving" and kind == "leave_sent":
        return cast(TerminalReason | None, state["terminal_reason"]) or "left_by_owner"
    if (
        phase == "leaving"
        and kind == "browser_failed"
        and state["terminal_reason"] in ("audio_failure", "conversation_failure")
    ):
        return cast(TerminalReason, state["terminal_reason"])
    if transition.pending is not None:
        return transition.pending
    return transition.reason


def apply_transition(
    state: LifecycleState,
    kind: LifecycleEventKind,
    attribution: LifecycleAttribution,
    *,
    previous: PublicPhase | None = None,
) -> LifecycleUpdate:
    if attribution not in _ATTRIBUTIONS:
        raise ValueError(f"unknown lifecycle attribution: {attribution}")
    phase = state["phase"]
    if phase in TERMINAL_PHASES:
        return {}
    if kind == "cancelled":
        return {"phase": "ended", "terminal_reason": "stopped_by_owner"}
    if kind == "reconnected":
        if phase != "joining" or previous is None:
            raise ValueError("reconnected without a previous phase")
        return {"phase": previous}
    transition = _TRANSITIONS.get((phase, kind))
    if transition is None:
        raise ValueError(f"invalid lifecycle event {kind!r} for phase {phase!r}")
    reason = _resolve_reason(state, phase, kind, transition)
    to_phase = _terminal_phase(reason) if transition.phase == "ended" else transition.phase
    update: LifecycleUpdate = {"phase": to_phase}
    if reason is not None:
        update["terminal_reason"] = reason
    if transition.effect is not None and all(
        item["effect"] != transition.effect for item in state["external_effects"]
    ):
        update["external_effects"] = [*state["external_effects"], {"effect": transition.effect}]
    validate_phase_reason(to_phase, update.get("terminal_reason", state["terminal_reason"]))
    return update


JoinAttemptOutcome = Literal["retry", "joined", "failed", "aborted"]
JOIN_RETRYABLE_STATUSES: frozenset[JoinStatus] = frozenset({"timeout", "failed"})
JOIN_BACKOFF_FACTOR = 2.0


class JoinRetryRequested(Exception):
    def __init__(self, result: JoinResult) -> None:
        super().__init__(f"join attempt ended with {result.status}")
        self.result = result


@dataclass(frozen=True, slots=True)
class JoinAttempt:
    number: int
    last: bool

    def retries(self, status: JoinStatus) -> bool:
        return not self.last and status in JOIN_RETRYABLE_STATUSES


class JoinStep(NamedTuple):
    result: JoinResult
    update: LifecycleUpdate


def join_retry_policy(*, max_attempts: int, base_seconds: float, cap_seconds: float) -> RetryPolicy:
    return RetryPolicy(
        initial_interval=base_seconds,
        backoff_factor=JOIN_BACKOFF_FACTOR,
        max_interval=cap_seconds,
        max_attempts=max_attempts,
        jitter=True,
        retry_on=JoinRetryRequested,
    )


DEFAULT_JOIN_RETRY = join_retry_policy(
    max_attempts=DEFAULTS.meeting.join_max_attempts,
    base_seconds=DEFAULTS.meeting.join_retry_base_seconds,
    cap_seconds=DEFAULTS.meeting.join_retry_cap_seconds,
)


def _join_outcome(status: JoinStatus, *, retrying: bool) -> JoinAttemptOutcome:
    if retrying:
        return "retry"
    if status == "admitted":
        return "joined"
    if status == "aborted":
        return "aborted"
    return "failed"


class Joiner(Protocol):
    def record(self, event: str, **fields: Any) -> None: ...

    async def join(self, state: LifecycleState, attempt: JoinAttempt) -> JoinStep: ...


class LifecycleDriver(Joiner, Protocol):
    async def prepare(self, state: LifecycleState) -> LifecycleUpdate: ...

    async def await_admission(self, state: LifecycleState) -> LifecycleUpdate: ...

    async def activate(self, state: LifecycleState) -> LifecycleUpdate: ...

    async def monitor(self, state: LifecycleState) -> LifecycleUpdate: ...

    async def leave(self, state: LifecycleState) -> LifecycleUpdate: ...


_SUCCESSORS: dict[str, str] = {
    "prepare": "join",
    "join": "await_admission",
    "await_admission": "activate",
    "activate": "monitor",
    "monitor": "leave",
    "leave": "finalize",
}
_NODE_NAMES = (*_SUCCESSORS, "finalize", "fail")


def _route(state: LifecycleState, node: str) -> str:
    if state["phase"] in TERMINAL_PHASES:
        return "finalize"
    if state["phase"] == "leaving":
        return "finalize" if node == "leave" else "leave"
    return _SUCCESSORS[node]


def _advance(node: str, state: LifecycleState, update: LifecycleUpdate) -> Command[Any]:
    merged = cast(LifecycleState, {**state, **update})
    return Command(update=update, goto=_route(merged, node))


_Step = Callable[[LifecycleDriver, LifecycleState], Awaitable[LifecycleUpdate]]


def _require_driver(
    runtime: Runtime[RuntimeContext | None], *, needs_browser: bool
) -> LifecycleDriver:
    context = runtime.context if runtime.context is not None else RuntimeContext()
    driver = cast(LifecycleDriver, context.require("session"))
    if needs_browser:
        context.require("browser")
    return driver


def _missing_handle(error: MissingRuntimeHandle) -> Command[Any]:
    return Command(update={"terminal_reason": f"runtime_handle_missing:{error.name}"}, goto="fail")


def _step_node(
    node: str, step: _Step, *, needs_browser: bool
) -> Callable[..., Awaitable[Command[Any]]]:
    async def run(
        state: LifecycleState, *, runtime: Runtime[RuntimeContext | None]
    ) -> Command[Any]:
        try:
            driver = _require_driver(runtime, needs_browser=needs_browser)
        except MissingRuntimeHandle as error:
            return _missing_handle(error)
        return _advance(node, state, await step(driver, state))

    return run


def _join_node(
    policy: RetryPolicy, joiner: Joiner | None
) -> Callable[..., Awaitable[Command[Any]]]:
    async def run(
        state: LifecycleState, *, runtime: Runtime[RuntimeContext | None]
    ) -> Command[Any]:
        try:
            driver = _require_driver(runtime, needs_browser=True)
        except MissingRuntimeHandle as error:
            return _missing_handle(error)
        active = joiner if joiner is not None else driver
        info = runtime.execution_info
        number = info.node_attempt if info is not None else 1
        attempt = JoinAttempt(number=number, last=number >= policy.max_attempts)
        step = await active.join(state, attempt)
        retrying = attempt.retries(step.result.status)
        active.record(
            "join_attempt_finished",
            source="runtime",
            attempt=number,
            outcome=_join_outcome(step.result.status, retrying=retrying),
        )
        if retrying:
            raise JoinRetryRequested(step.result)
        return _advance("join", state, step.update)

    return run


async def _finalize(state: LifecycleState) -> LifecycleUpdate:
    if state["phase"] in TERMINAL_PHASES:
        return {}
    reason = state["terminal_reason"] or "infra_failure"
    return {"phase": _terminal_phase(reason), "terminal_reason": reason}


async def _fail(state: LifecycleState) -> LifecycleUpdate:
    reason = state["terminal_reason"] or "infra_failure"
    return {"phase": _terminal_phase(reason), "terminal_reason": reason}


def build_lifecycle_graph(
    checkpointer: BaseCheckpointSaver[Any],
    *,
    join_retry: RetryPolicy = DEFAULT_JOIN_RETRY,
    joiner: Joiner | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    builder: StateGraph[LifecycleState, RuntimeContext, LifecycleState, LifecycleState] = (
        StateGraph(LifecycleState, context_schema=RuntimeContext)
    )
    builder.add_node(
        "prepare",
        _step_node("prepare", lambda driver, state: driver.prepare(state), needs_browser=False),
    )
    builder.add_node("join", _join_node(join_retry, joiner), retry_policy=join_retry)
    builder.add_node(
        "await_admission",
        _step_node(
            "await_admission",
            lambda driver, state: driver.await_admission(state),
            needs_browser=True,
        ),
    )
    builder.add_node(
        "activate",
        _step_node("activate", lambda driver, state: driver.activate(state), needs_browser=True),
    )
    builder.add_node(
        "monitor",
        _step_node("monitor", lambda driver, state: driver.monitor(state), needs_browser=True),
    )
    builder.add_node(
        "leave", _step_node("leave", lambda driver, state: driver.leave(state), needs_browser=True)
    )
    builder.add_node("finalize", _finalize)
    builder.add_node("fail", _fail)
    builder.add_edge(START, "prepare")
    builder.add_edge("finalize", END)
    builder.add_edge("fail", END)
    return builder.compile(checkpointer=checkpointer)


_RESUME_TARGETS: dict[PublicPhase, str] = {
    "created": "prepare",
    "joining": "join",
    "lobby": "await_admission",
    "admitted": "activate",
    "active": "monitor",
    "leaving": "leave",
}


def _thread_config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


async def resume_lifecycle(
    graph: CompiledStateGraph[Any, Any, Any, Any], thread_id: str, context: RuntimeContext
) -> LifecycleState:
    config = _thread_config(thread_id)
    saved = await graph.aget_state(config)
    if not saved.values:
        raise ValueError(f"no lifecycle checkpoint for thread {thread_id!r}")
    phase = saved.values["phase"]
    if phase in TERMINAL_PHASES:
        raise ValueError(f"cannot resume lifecycle in phase {phase!r}")
    target = _RESUME_TARGETS[phase]
    await graph.aupdate_state(config, {"memory_evidence": []}, as_node=target)
    command: Command[Any] = Command(goto=target)
    resumed: object = await graph.ainvoke(command, config, context=context)
    return cast(LifecycleState, resumed)


@dataclass(frozen=True, slots=True)
class CheckpointDegradation:
    send_timeout_seconds: float = DEFAULTS.database.send_timeout_seconds
    faults: FaultReporter | None = None
    journal: Journal | None = None


_DEGRADABLE_PHASES: tuple[PublicPhase, ...] = ("admitted", "active")
_EVENT_LABEL = "lifecycle_event"


def _checkpoint_config(config: RunnableConfig, checkpoint: Checkpoint) -> RunnableConfig:
    configurable = dict(config.get("configurable") or {})
    configurable["checkpoint_id"] = checkpoint["id"]
    return {"configurable": configurable}


class _QueuedPut(NamedTuple):
    config: RunnableConfig
    checkpoint: Checkpoint
    metadata: CheckpointMetadata
    new_versions: ChannelVersions


_PutQueue = list[_QueuedPut]


class DegradingCheckpointer(BaseCheckpointSaver[Any]):
    def __init__(self, inner: BaseCheckpointSaver[Any], policy: CheckpointDegradation) -> None:
        super().__init__(serde=inner.serde)
        self._inner = inner
        self._policy = policy
        self._queued: dict[str, _PutQueue] = {}

    @override
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self._inner.get_tuple(config)

    @override
    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        return self._inner.list(config, filter=filter, before=before, limit=limit)

    @override
    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self._inner.put(config, checkpoint, metadata, new_versions)

    @override
    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self._inner.put_writes(config, writes, task_id, task_path)

    @override
    def delete_thread(self, thread_id: str) -> None:
        self._queued.pop(thread_id, None)
        self._inner.delete_thread(thread_id)

    @override
    def get_next_version(self, current: Any, channel: None) -> Any:
        return self._inner.get_next_version(current, channel)

    @override
    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        queued = self._queued.get(_thread_of(config))
        if queued:
            latest = queued[-1]
            return CheckpointTuple(config, latest.checkpoint, latest.metadata)
        return await self._inner.aget_tuple(config)

    @override
    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        async for item in self._inner.alist(config, filter=filter, before=before, limit=limit):
            yield item

    @override
    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread = _thread_of(config)
        current = _QueuedPut(config, checkpoint, metadata, new_versions)
        queue: _PutQueue = [*self._queued.get(thread, []), current]
        parent = await self._durable_parent(config) if len(queue) > 1 else None
        written: RunnableConfig = _checkpoint_config(config, checkpoint)
        while queue:
            item = queue[0]
            target = item.config if parent is None else _with_parent(item.config, parent)
            try:
                async with asyncio.timeout(self._policy.send_timeout_seconds):
                    written = await self._inner.aput(
                        target, item.checkpoint, item.metadata, item.new_versions
                    )
            except Exception as error:
                if not is_checkpoint_error(error):
                    raise
                if not await self._landed(item):
                    phase = await self._phase_before(thread)
                    if phase not in _DEGRADABLE_PHASES:
                        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE) from error
                    self._queued[thread] = queue
                    self._record_degraded(item.config, error)
                    return _checkpoint_config(config, checkpoint)
                written = _checkpoint_config(item.config, item.checkpoint)
            queue.pop(0)
            parent = item.checkpoint["id"]
        self._queued.pop(thread, None)
        return written

    async def _durable_parent(self, config: RunnableConfig) -> str | None:
        durable = await self._read_durable(config)
        return durable.checkpoint["id"] if durable is not None else None

    async def _landed(self, item: _QueuedPut) -> bool:
        durable = await self._read_durable(item.config)
        return durable is not None and durable.checkpoint["id"] == item.checkpoint["id"]

    async def _phase_before(self, thread: str) -> PublicPhase | None:
        previous = self._queued.get(thread)
        if previous:
            checkpoint: Checkpoint | None = previous[-1].checkpoint
        else:
            durable = await self._read_durable({"configurable": {"thread_id": thread}})
            checkpoint = durable.checkpoint if durable is not None else None
        if checkpoint is None:
            return None
        return cast(PublicPhase, checkpoint["channel_values"].get("phase"))

    async def _read_durable(self, config: RunnableConfig) -> CheckpointTuple | None:
        try:
            return await self._inner.aget_tuple(_latest_config(config))
        except Exception as error:
            if not is_checkpoint_error(error):
                raise
            raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE) from error

    def _record_degraded(self, config: RunnableConfig, error: Exception) -> None:
        detail = f"lifecycle persist queued: {type(error).__name__}"
        kind: FaultKind = "timeout" if isinstance(error, TimeoutError) else "unavailable"
        label = (config.get("configurable") or {}).get(_EVENT_LABEL, "checkpoint")
        if self._policy.faults is not None:
            self._policy.faults.record(Fault(source="runtime", kind=kind, detail=detail))
        if self._policy.journal is not None:
            self._policy.journal.record(
                "lifecycle_persist_queued",
                source="runtime",
                kind=label,
                error_type=type(error).__name__,
                detail=detail,
            )

    @override
    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        if self._queued.get(_thread_of(config)):
            return
        await self._inner.aput_writes(config, writes, task_id, task_path)

    @override
    async def adelete_thread(self, thread_id: str) -> None:
        self._queued.pop(thread_id, None)
        await self._inner.adelete_thread(thread_id)


def _with_parent(config: RunnableConfig, parent: str | None) -> RunnableConfig:
    configurable = {
        "checkpoint_ns": "",
        **(config.get("configurable") or {}),
        "checkpoint_id": parent,
    }
    return {"configurable": configurable}


def _latest_config(config: RunnableConfig) -> RunnableConfig:
    configurable = {
        key: value
        for key, value in (config.get("configurable") or {}).items()
        if key in ("thread_id", "checkpoint_ns")
    }
    return {"configurable": configurable}


def _thread_of(config: RunnableConfig) -> str:
    return str((config.get("configurable") or {})["thread_id"])


@dataclass(frozen=True, slots=True)
class LifecycleThread:
    graph: CompiledStateGraph[Any, Any, Any, Any]
    thread_id: str
    initial: LifecycleState

    @property
    def config(self) -> RunnableConfig:
        return _thread_config(self.thread_id)

    async def read(self) -> LifecycleState:
        saved = await self.graph.aget_state(self.config)
        return cast(LifecycleState, saved.values)

    async def send(self, kind: LifecycleEventKind, attribution: LifecycleAttribution) -> None:
        state = await self.read()
        update = apply_transition(state, kind, attribution)
        if update:
            labelled: RunnableConfig = {
                "configurable": {"thread_id": self.thread_id, _EVENT_LABEL: kind}
            }
            await self.graph.aupdate_state(labelled, update, as_node="finalize")


async def initialize_lifecycle(thread: LifecycleThread, *, resume: bool) -> LifecycleState:
    initial = thread.initial
    saved = await thread.read()
    if not saved:
        await thread.graph.aupdate_state(thread.config, initial, as_node="finalize")
        return initial
    for field in ("owner_id", "twin_id", "meeting_id"):
        if saved[field] != initial[field]:
            raise FakeYouError(ErrorCode.MEETING_CHECKPOINT_IDENTITY_MISMATCH)
    phase = saved["phase"]
    if phase not in TERMINAL_PHASES and phase != "created" and not resume:
        raise FakeYouError(
            ErrorCode.MEETING_CANNOT_RESUME_PHASE,
            meeting_id=saved["meeting_id"],
            phase=phase,
        )
    return saved
