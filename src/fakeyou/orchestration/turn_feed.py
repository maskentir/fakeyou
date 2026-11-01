import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import partial

from fakeyou.conversation.turn_ingress import TurnIngress
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.faults import (
    ConsumerSink,
    FaultReporter,
    guard_consumer,
    record_consumer_fault,
)
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.early_turns import EarlyTurns
from fakeyou.orchestration.kernel_port import (
    HeardChat,
    HeardFinal,
    HeardInterim,
    HeardVoice,
    KernelPort,
    TurnSignal,
)

Work = Callable[[], Awaitable[None]]
FinalObserver = Callable[[TranscriptEvent], Awaitable[None]]


class _Lane:
    def __init__(self, name: str, sink: ConsumerSink) -> None:
        self._name = name
        self._sink = sink
        self._work: asyncio.Queue[Work] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def put(self, work: Work) -> None:
        if self._closed:
            return
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"turn-feed-{self._name}")
        self._work.put_nowait(work)

    async def idle(self) -> None:
        await self._work.join()

    async def close(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        detail = f"turn_feed_{self._name}"
        while True:
            work = await self._work.get()
            try:
                await guard_consumer(work, sink=self._sink, source="agent", detail=detail)
            except asyncio.CancelledError as error:
                current = asyncio.current_task()
                if current is None or current.cancelling():
                    raise
                record_consumer_fault(self._sink, source="agent", detail=detail, error=error)
            finally:
                self._work.task_done()


class _EarlyAnswering:
    def __init__(self, ingress: TurnIngress, feed: "TurnFeed") -> None:
        self._ingress = ingress
        self._feed = feed

    async def ready_to_answer(self, text: str) -> bool:
        return await self._ingress.ready_to_answer(text)

    async def receive_final(self, event: TranscriptEvent) -> None:
        await self._feed.answer(event)

    async def withdraw(self, *, reason: str) -> None:
        await self._ingress.withdraw(reason=reason)


@dataclass(frozen=True, slots=True)
class EarlyCommit:
    hold_seconds: float
    build_event: Callable[[str], TranscriptEvent]


class TurnFeed:
    def __init__(
        self,
        ingress: TurnIngress,
        *,
        journal: Journal,
        faults: FaultReporter | None = None,
        early: EarlyCommit | None = None,
        on_final: FinalObserver | None = None,
    ) -> None:
        sink = ConsumerSink(faults, journal)
        self._ingress = ingress
        self._on_final = on_final
        self._voice = _Lane("voice", sink)
        self._finals = _Lane("finals", sink)
        self._moments = _Lane("moments", sink)
        self._early = (
            EarlyTurns(
                _EarlyAnswering(ingress, self),
                hold_seconds=early.hold_seconds,
                build_event=early.build_event,
                journal=journal,
            )
            if early is not None
            else None
        )

    def attach(self, kernel: KernelPort) -> None:
        kernel.on_turn(self)

    def accept(self, signal: TurnSignal) -> None:
        match signal:
            case HeardVoice(active=active):
                self._voice.put(partial(self._voice_edge, active=active))
            case HeardInterim(text=text):
                self._voice.put(partial(self._interim, text))
            case HeardFinal(event=event):
                self._voice.put(partial(self._settle, event))
            case HeardChat(event=event):
                self._voice.put(partial(self._queue_chat, event))

    async def answer(self, event: TranscriptEvent) -> None:
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._finals.put(partial(self._answer_early, event, done))
        await done

    async def idle(self) -> None:
        for lane in (self._voice, self._finals, self._moments):
            await lane.idle()

    async def close(self) -> None:
        for lane in (self._voice, self._finals, self._moments):
            await lane.close()
        if self._early is not None:
            await self._early.close()

    async def _voice_edge(self, *, active: bool) -> None:
        if self._early is not None:
            self._early.on_speech(active=active)

    async def _interim(self, text: str) -> None:
        await self._ingress.handle_interim(text)
        if self._early is not None:
            await self._early.on_interim(text)

    async def _settle(self, event: TranscriptEvent) -> None:
        swallowed = self._early is not None and await self._early.settle(event)
        if swallowed:
            self._observe_moment(event)
        else:
            self._finals.put(partial(self._final, event))

    async def _queue_chat(self, event: TranscriptEvent) -> None:
        self._finals.put(partial(self._chat, event))

    async def _final(self, event: TranscriptEvent) -> None:
        try:
            await self._ingress.receive_final(event)
        finally:
            self._observe_moment(event)

    async def _answer_early(self, event: TranscriptEvent, done: asyncio.Future[None]) -> None:
        try:
            await self._ingress.receive_final(event)
        finally:
            if not done.done():
                done.set_result(None)

    def _observe_moment(self, event: TranscriptEvent) -> None:
        if self._on_final is not None:
            self._moments.put(partial(self._on_final, event))

    async def _chat(self, event: TranscriptEvent) -> None:
        self._ingress.receive_chat(event)
