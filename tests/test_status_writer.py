from datetime import UTC, datetime
from typing import override

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.db.session_status import SessionStatus
from fakeyou.foundation.taxonomy import PublicPhase, TerminalReason
from fakeyou.worker.status_writer import StatusWriter
from tests._session_status_fakes import MemorySessionStatus

AT = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
LIVE: tuple[PublicPhase, ...] = ("joining", "lobby", "admitted", "active")


class RecordingStatus(MemorySessionStatus):
    def __init__(self) -> None:
        super().__init__()
        self.written: list[str] = []

    @override
    async def record_phase(
        self,
        meeting_id: str,
        phase: PublicPhase,
        at: datetime,
        *,
        terminal_reason: TerminalReason | None = None,
        detail: str | None = None,
    ) -> SessionStatus | None:
        self.written.append(phase)
        return await super().record_phase(
            meeting_id, phase, at, terminal_reason=terminal_reason, detail=detail
        )


class UnreachableStatus(MemorySessionStatus):
    @override
    async def record_phase(
        self,
        meeting_id: str,
        phase: PublicPhase,
        at: datetime,
        *,
        terminal_reason: TerminalReason | None = None,
        detail: str | None = None,
    ) -> SessionStatus | None:
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE)


async def test_phases_are_written_in_the_order_they_happened() -> None:
    status = RecordingStatus()
    await status.create("m1", "owner-1", AT)
    writer = StatusWriter(status, "m1")

    for phase in LIVE:
        writer.note(phase)
    await writer.write("ended", terminal_reason="stopped_by_owner", detail="owner stop")
    await writer.close()

    assert status.written == [*LIVE, "ended"]
    final = await status.get("m1")
    assert final is not None
    assert (final.terminal_reason, final.detail) == ("stopped_by_owner", "owner stop")


async def test_an_unreachable_database_never_breaks_the_session() -> None:
    writer = StatusWriter(UnreachableStatus(), "m1")

    assert await writer.write("active") is True
    await writer.close()


async def test_a_repeated_live_phase_is_written_once() -> None:
    writer = StatusWriter(MemorySessionStatus(), "m1")

    assert writer.note("active") is True
    assert writer.note("active") is False
    await writer.close()


class FlakyStatus(MemorySessionStatus):
    def __init__(self, failures: int, error: Exception) -> None:
        super().__init__()
        self.failures = failures
        self.error = error

    @override
    async def record_phase(
        self,
        meeting_id: str,
        phase: PublicPhase,
        at: datetime,
        *,
        terminal_reason: TerminalReason | None = None,
        detail: str | None = None,
    ) -> SessionStatus | None:
        if self.failures:
            self.failures -= 1
            raise self.error
        return await super().record_phase(
            meeting_id, phase, at, terminal_reason=terminal_reason, detail=detail
        )


async def test_an_unexpected_error_does_not_stop_later_writes() -> None:
    status = FlakyStatus(1, RuntimeError("relation does not exist"))
    await status.create("m1", "owner-1", AT)
    writer = StatusWriter(status, "m1", retry_seconds=())

    await writer.write("lobby")
    await writer.write("active")
    await writer.close()

    final = await status.get("m1")
    assert final is not None
    assert final.phase == "active"


async def test_a_terminal_write_is_retried_until_it_lands() -> None:
    status = FlakyStatus(2, FakeYouError(ErrorCode.DATABASE_UNREACHABLE))
    await status.create("m1", "owner-1", AT)
    writer = StatusWriter(status, "m1", retry_seconds=(0.0, 0.0, 0.0))

    await writer.write("ended", terminal_reason="ended_by_host", detail="host ended the call")
    await writer.close()

    final = await status.get("m1")
    assert final is not None
    assert (final.phase, final.terminal_reason) == ("ended", "ended_by_host")


async def test_a_live_write_is_not_retried() -> None:
    status = FlakyStatus(1, RuntimeError("relation does not exist"))
    await status.create("m1", "owner-1", AT)
    writer = StatusWriter(status, "m1", retry_seconds=(0.0, 0.0, 0.0))

    await writer.write("lobby")
    await writer.close()

    final = await status.get("m1")
    assert final is not None
    assert final.phase == "created"
