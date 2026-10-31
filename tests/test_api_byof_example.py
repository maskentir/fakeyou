import importlib.util
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import override

import httpx2
import pytest

from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.db.session_status import DesiredState, PhaseUpdate, SessionStatus
from fakeyou.observability.journal import journal_path
from tests._session_status_fakes import MemorySessionStatus
from tests.test_api_events import ApiEventsHarness, SeededTransport
from tests.test_api_sessions import STATUS_AT

EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "examples" / "byof-client" / "client.py"
LINK = "https://meet.google.com/abc-defg-hij"
OWNER_TOKEN = "owner-secret"


class _RoutedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, *, plain: httpx2.AsyncBaseTransport, streaming: httpx2.AsyncBaseTransport):
        self._plain = plain
        self._streaming = streaming

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        target = self._streaming if request.url.path.endswith("/events") else self._plain
        return await target.handle_async_request(request)


def _routed(api: ApiEventsHarness) -> httpx2.AsyncBaseTransport:
    return _RoutedTransport(plain=httpx2.ASGITransport(app=api.app), streaming=api.stream_transport)


@pytest.fixture
def example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("byof_client", EXAMPLE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _JobRecordingTransport(SeededTransport):
    def __init__(self) -> None:
        super().__init__()
        self.jobs: list[tuple[str, dict[str, str]]] = []

    async def publish_job(self, stream: str, fields: Mapping[str, str]) -> str:
        self.jobs.append((stream, dict(fields)))
        return f"{len(self.jobs)}-0"


class _FinishingStatus(MemorySessionStatus):
    def __init__(self, on_join: Callable[[str], Awaitable[None]]) -> None:
        super().__init__()
        self._on_join = on_join

    @override
    async def desire(
        self, meeting_id: str, state: DesiredState, at: datetime
    ) -> SessionStatus | None:
        wanted = await super().desire(meeting_id, state, at)
        if wanted is not None and state == "joined":
            await self._on_join(meeting_id)
        return wanted


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[ApiEventsHarness]:
    harness = ApiEventsHarness(settings)

    async def finish_session(meeting_id: str) -> None:
        harness.seeded.seed_events(
            meeting_id,
            [
                ("1-0", "session_started", {"phase": "joining"}),
                (
                    "2-0",
                    "session_ended",
                    {"phase": "ended", "terminal_reason": "stopped_by_owner"},
                ),
            ],
        )
        await harness.status.record_phase(
            meeting_id, PhaseUpdate("ended", STATUS_AT, terminal_reason="stopped_by_owner")
        )
        path = journal_path(harness.settings.paths.journal_dir, SessionId(meeting_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "event": "session_finished",
                    "meeting_id": meeting_id,
                    "at": "2026-10-03T12:00:00+00:00",
                    "phase": "ended",
                    "terminal_reason": "stopped_by_owner",
                    "exit_code": 0,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    harness.seeded = _JobRecordingTransport()
    harness.transport = harness.seeded
    harness.status = _FinishingStatus(finish_session)
    try:
        yield harness
    finally:
        await harness.aclose()


def test_byof_example_does_not_import_the_fakeyou_package() -> None:
    source = EXAMPLE_PATH.read_text(encoding="utf-8")

    assert "import fakeyou" not in source
    assert "from fakeyou" not in source


async def test_byof_example_flow_runs_against_the_real_app(
    example: ModuleType, api: ApiEventsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(example, "POLL_ATTEMPTS", 1)
    monkeypatch.setattr(example, "POLL_INTERVAL_SECONDS", 0.0)
    output = await example.run(
        base_url="http://testserver",
        link=LINK,
        token=OWNER_TOKEN,
        transport=_routed(api),
    )

    assert output["phase"] == "ended"
    assert output["terminal_reason"] == "stopped_by_owner"
    assert output["ingestion"]["status"] in {"pending", "completed"}
    assert output["steps"] == ["create", "join", "events", "stop", "ingest", "debrief", "report"]
    assert output["events_received"] == 2
    assert output["report"]["meeting_id"] == output["meeting_id"]


async def test_byof_example_reports_a_named_error_on_a_rejected_token(
    example: ModuleType, api: ApiEventsHarness
) -> None:
    with pytest.raises(example.ExampleError) as caught:
        await example.run(
            base_url="http://testserver",
            link=LINK,
            token="wrong-token",
            transport=_routed(api),
        )

    assert caught.value.code == "api.unauthorized"
