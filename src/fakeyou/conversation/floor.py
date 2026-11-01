import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from time import monotonic
from typing import Any


@dataclass(slots=True)
class ActiveSpeech:
    task: asyncio.Task[None]
    generation: int
    started: float
    transcript_id: str | None
    request_id: str | None = None


class SpeechFloor:
    def __init__(self, generation: int = 0, *, clock: Callable[[], float] = monotonic) -> None:
        self.generation = generation
        self.last_reason = ""
        self._clock = clock
        self._active: ActiveSpeech | None = None

    @property
    def busy(self) -> bool:
        return self._active is not None

    @property
    def active(self) -> ActiveSpeech | None:
        return self._active

    def now(self) -> float:
        return self._clock()

    def admits(self, generation: int) -> bool:
        return generation == self.generation

    def advance(self) -> int:
        self.generation += 1
        return self.generation

    async def hold(
        self,
        work: Coroutine[Any, Any, None],
        *,
        transcript_id: str | None = None,
        request_id: str | None = None,
        generation: int | None = None,
    ) -> bool:
        if self.busy:
            work.close()
            raise RuntimeError("a turn already holds the floor")
        task = asyncio.ensure_future(work)
        active = ActiveSpeech(
            task=task,
            generation=self.generation if generation is None else generation,
            started=self.now(),
            transcript_id=transcript_id,
            request_id=request_id,
        )
        self._active = active
        try:
            await task
        except asyncio.CancelledError:
            caller = asyncio.current_task()
            if caller is not None and caller.cancelling() > 0:
                raise
            return False
        finally:
            if self._active is active:
                self._active = None
        return True

    async def interrupt(self, reason: str = "") -> int:
        self.advance()
        self.last_reason = reason
        active = self._active
        if active is not None and not active.task.done():
            active.task.cancel()
            await asyncio.gather(active.task, return_exceptions=True)
        return self.generation


async def interrupt_speech(floor: SpeechFloor | None, generation: int) -> int:
    if floor is None:
        return generation + 1
    return await floor.interrupt()
