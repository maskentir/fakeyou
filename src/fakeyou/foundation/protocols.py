from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from fakeyou.foundation.transcript import TranscriptEvent


@runtime_checkable
class SupportsAclose(Protocol):
    async def aclose(self) -> None: ...


@runtime_checkable
class SupportsAgentState(Protocol):
    async def aget_state(self, config: Mapping[str, Any]) -> Any: ...

    async def aupdate_state(
        self,
        config: Mapping[str, Any],
        values: Mapping[str, Any],
        as_node: str | None = None,
    ) -> None: ...


@runtime_checkable
class SupportsPregenSentences(Protocol):
    def stream_pregen_sentences(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
    ) -> AsyncIterator[str]: ...


@runtime_checkable
class SupportsCompleteAdoptedPregen(Protocol):
    async def complete_adopted_pregen(
        self, events: Sequence[TranscriptEvent], sentences: Sequence[str]
    ) -> None: ...


@runtime_checkable
class SupportsReconnectWhenIdle(Protocol):
    async def reconnect_when_idle(self) -> None: ...


@runtime_checkable
class SupportsIdleSeconds(Protocol):
    def idle_seconds(self, now: float) -> float | None: ...


@runtime_checkable
class SupportsLastCapture(Protocol):
    @property
    def last_capture(self) -> tuple[int, float] | None: ...
