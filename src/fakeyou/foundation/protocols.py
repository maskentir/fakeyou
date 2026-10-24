from typing import Protocol, runtime_checkable


@runtime_checkable
class SupportsAclose(Protocol):
    async def aclose(self) -> None: ...


@runtime_checkable
class SupportsReconnectWhenIdle(Protocol):
    async def reconnect_when_idle(self) -> None: ...


@runtime_checkable
class SupportsIdleSeconds(Protocol):
    def idle_seconds(self, now: float) -> float | None: ...


@runtime_checkable
class SupportsSessionExpiry(Protocol):
    def session_expires_in(self, now: float) -> float | None: ...
