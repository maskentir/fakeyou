import asyncio
import inspect
from collections.abc import Coroutine
from typing import Any, override

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from fakeyou.conversation.floor import FloorHeld, SpeechFloor

TIMEOUT_SECONDS = 2


async def held_until(gate: asyncio.Event) -> None:
    await gate.wait()


class FloorModel(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.runner = asyncio.Runner()
        self.floor = SpeechFloor(generation=0)
        self.generation = 0
        self.turn: asyncio.Task[bool] | None = None
        self.gate: asyncio.Event | None = None

    def run(self, awaitable: Coroutine[Any, Any, Any]) -> Any:
        return self.runner.run(awaitable)

    def settle(self) -> None:
        self.run(asyncio.sleep(0))

    def _finish_turn(self) -> bool | None:
        if self.turn is None:
            return None
        outcome: bool = self.run(asyncio.wait_for(self.turn, timeout=TIMEOUT_SECONDS))
        self.turn = None
        self.gate = None
        return outcome

    @rule()
    def begin_turn(self) -> None:
        if self.turn is not None:
            return
        gate = asyncio.Event()
        self.gate = gate
        self.turn = self.runner.get_loop().create_task(self.floor.hold(held_until(gate)))
        self.settle()
        assert self.floor.busy

    @precondition(lambda self: self.turn is not None)
    @rule()
    def second_turn_is_refused(self) -> None:
        refused = held_until(asyncio.Event())
        try:
            self.run(self.floor.hold(refused))
        except FloorHeld:
            assert inspect.getcoroutinestate(refused) == inspect.CORO_CLOSED
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

        async def interrupt_and_retake() -> bool:
            await self.floor.interrupt("retake")
            return await self.floor.hold(held_until(gate))

        retaken = self.runner.get_loop().create_task(interrupt_and_retake())
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
        self.runner.close()


TestFloor = FloorModel.TestCase
TestFloor.settings = settings(max_examples=200, stateful_step_count=40, deadline=None)


async def test_a_turn_finished_before_interrupt_frees_the_floor_for_the_next_turn() -> None:
    floor = SpeechFloor(generation=0)
    gate = asyncio.Event()
    turn = asyncio.create_task(floor.hold(held_until(gate)))
    await asyncio.sleep(0)
    gate.set()
    await turn
    assert floor.busy is False

    await floor.interrupt("late")

    retake = asyncio.create_task(floor.hold(asyncio.sleep(0)))
    assert await asyncio.wait_for(retake, timeout=TIMEOUT_SECONDS) is True


async def test_an_interrupt_in_the_gap_after_a_turn_ended_still_frees_the_floor() -> None:
    floor = SpeechFloor(generation=0)
    gate = asyncio.Event()
    turn = asyncio.create_task(floor.hold(held_until(gate)))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.sleep(0)
    assert floor.active is not None
    assert floor.active.task.done()
    assert turn.done() is False

    await floor.interrupt("gap")

    assert await asyncio.wait_for(floor.hold(asyncio.sleep(0)), timeout=TIMEOUT_SECONDS) is True
