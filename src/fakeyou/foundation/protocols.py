from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


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
class SupportsReconnectWhenIdle(Protocol):
    async def reconnect_when_idle(self) -> None: ...


@runtime_checkable
class SupportsIdleSeconds(Protocol):
    def idle_seconds(self, now: float) -> float | None: ...


@runtime_checkable
class SupportsSessionExpiry(Protocol):
    def session_expires_in(self, now: float) -> float | None: ...
