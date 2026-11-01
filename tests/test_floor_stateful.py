import asyncio
from collections.abc import Coroutine
from typing import Any, override

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from fakeyou.conversation.floor import SpeechFloor


class FloorModel(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.floor = SpeechFloor(generation=0)
        self.generation = 0
        self.turn: asyncio.Task[bool] | None = None
        self.gate: asyncio.Event | None = None

    def run(self, awaitable: Coroutine[Any, Any, Any]) -> Any:
        return self.loop.run_until_complete(awaitable)

    def settle(self) -> None:
        self.run(asyncio.sleep(0))

    def _finish_turn(self) -> bool | None:
        if self.turn is None:
            return None
        outcome: bool = self.run(asyncio.wait_for(self.turn, timeout=2))
        self.turn = None
        self.gate = None
        return outcome

    @rule()
    def begin_turn(self) -> None:
        if self.turn is not None:
            return
        gate = asyncio.Event()

        async def work() -> None:
            await gate.wait()

        self.gate = gate
        self.turn = self.loop.create_task(self.floor.hold(work()))
        self.settle()
        assert self.floor.busy

    @precondition(lambda self: self.turn is not None)
    @rule()
    def second_turn_is_refused(self) -> None:
        async def work() -> None:
            return None

        coroutine = work()
        try:
            self.run(self.floor.hold(coroutine))
        except RuntimeError:
            coroutine.close()
            return
        raise AssertionError("a second turn was accepted while the floor was held")

    @precondition(lambda self: self.turn is not None)
    @rule()
    def release_turn(self) -> None:
        assert self.gate is not None
        self.gate.set()
        assert self._finish_turn() is True
        assert not self.floor.busy

    @precondition(lambda self: self.turn is not None)
    @rule()
    def interrupt_turn(self) -> None:
        self.generation += 1
        self.run(self.floor.interrupt("test"))
        assert self.floor.active is None
        assert self._finish_turn() is False

    @precondition(lambda self: self.turn is None)
    @rule()
    def interrupt_idle(self) -> None:
        self.generation += 1
        self.run(self.floor.interrupt("idle"))
        assert self.floor.active is None

    @precondition(lambda self: self.turn is not None)
    @rule()
    def interrupt_then_take_floor_at_once(self) -> None:
        self.generation += 1
        gate = asyncio.Event()

        async def work() -> None:
            await gate.wait()

        async def interrupt_and_retake() -> bool:
            await self.floor.interrupt("retake")
            return await self.floor.hold(work())

        retaken = self.loop.create_task(interrupt_and_retake())
        self.settle()
        assert self._finish_turn() is False
        self.gate = gate
        self.turn = retaken
        self.settle()
        assert self.floor.busy

    @rule()
    def advance(self) -> None:
        self.generation += 1
        assert self.floor.advance() == self.generation

    @rule(offset=st.integers(min_value=-1, max_value=2))
    def admission_matches_generation(self, offset: int) -> None:
        candidate = self.generation + offset
        assert self.floor.admits(candidate) == (candidate == self.generation)

    @invariant()
    def generation_is_the_model_generation(self) -> None:
        assert self.floor.generation == self.generation

    @invariant()
    def busy_means_one_live_turn(self) -> None:
        live = self.turn is not None and not self.turn.done()
        assert self.floor.busy == live

    @override
    def teardown(self) -> None:
        if self.turn is not None:
            self.turn.cancel()
            self.loop.run_until_complete(asyncio.gather(self.turn, return_exceptions=True))
        self.loop.close()


TestFloor = FloorModel.TestCase
TestFloor.settings = settings(max_examples=200, stateful_step_count=40, deadline=None)
