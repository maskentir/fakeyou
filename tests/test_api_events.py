import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, cast, override

import httpx2
import pytest
from pydantic import SecretStr

from fakeyou.api.app import ApiDeps, create_app
from fakeyou.api.schemas import SessionCreate, build_snapshot
from fakeyou.api.sessions import SessionStore
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.foundation.taxonomy import PublicPhase, TerminalReason
from tests._session_status_fakes import MemorySessionStatus
from tests.test_api_sessions import (
    ADMIN_TOKEN,
    AT,
    BASE_URL,
    OWNER_TOKEN,
    SESSION_PAYLOAD,
    STATUS_AT,
    ApiHarness,
    RecordingTransport,
)

KEEPALIVE_SECONDS = 0.05
CANCEL_TIMEOUT_SECONDS = 1.0
SETTLE_STEPS = 50
SETTLE_SECONDS = 0.01


def _stream_key(entry_id: str) -> tuple[int, int]:
    milliseconds, _, sequence = entry_id.partition("-")
    return int(milliseconds), int(sequence or "0")


class SeededTransport(RecordingTransport):
    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[str, list[tuple[str, dict[str, str]]]] = {}

    def seed_events(self, meeting_id: str, entries: list[tuple[str, str, dict[str, str]]]) -> None:
        self.seed_raw_events(
            meeting_id,
            [
                (
                    entry_id,
                    {
                        "v": "1",
                        "type": event_type,
                        "at": AT,
                        "meeting_id": meeting_id,
                        **fields,
                    },
                )
                for entry_id, event_type, fields in entries
            ],
        )

    def seed_raw_events(self, meeting_id: str, entries: list[tuple[str, dict[str, str]]]) -> None:
        stored = self.entries.setdefault(meeting_id, [])
        stored.extend((entry_id, dict(fields)) for entry_id, fields in entries)
        stored.sort(key=lambda item: _stream_key(item[0]))

    async def read_events(
        self, meeting_id: str, *, after: str, count: int = 100
    ) -> list[tuple[str, dict[str, str]]]:
        threshold = _stream_key(after)
        entries = self.entries.get(meeting_id, [])
        return [
            (entry_id, dict(fields))
            for entry_id, fields in entries
            if _stream_key(entry_id) > threshold
        ][:count]


class _StreamingBodyStream(httpx2.AsyncByteStream):
    def __init__(
        self,
        queue: asyncio.Queue[dict[str, Any]],
        task: asyncio.Task[None],
        transport: "_StreamingASGITransport",
    ) -> None:
        self._queue = queue
        self._task = task
        self._transport = transport

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            message = await self._queue.get()
            if message["type"] == "app.done":
                error = message["error"]
                if error is not None:
                    raise error
                return
            if message["type"] != "http.response.body":
                continue
            body = message.get("body", b"")
            if body:
                yield body
            if not message.get("more_body", False):
                return

    @override
    async def aclose(self) -> None:
        self._transport.close()
        try:
            await asyncio.wait_for(self._task, timeout=CANCEL_TIMEOUT_SECONDS)
        except TimeoutError:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        except asyncio.CancelledError:
            pass


class _StreamingASGITransport(httpx2.AsyncBaseTransport):
    def __init__(self, app: Any) -> None:
        self.app: Any = app
        self.tasks: list[asyncio.Task[None]] = []
        self._disconnect = asyncio.Event()

    def close(self) -> None:
        self._disconnect.set()

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        disconnect = self._disconnect = asyncio.Event()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def receive() -> dict[str, Any]:
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            await queue.put(message)

        def finished(task: asyncio.Task[None]) -> None:
            error: BaseException | None = None
            if not task.cancelled():
                error = task.exception()
            queue.put_nowait({"type": "app.done", "error": error})

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": request.method,
            "headers": [(name.lower(), value) for (name, value) in request.headers.raw],
            "scheme": request.url.scheme,
            "path": request.url.path,
            "raw_path": request.url.raw_path.split(b"?")[0],
            "query_string": request.url.query,
            "server": (request.url.host, request.url.port),
            "client": ("127.0.0.1", 123),
            "root_path": "",
        }
        task: asyncio.Task[None] = asyncio.create_task(self.app(scope, receive, send))
        self.tasks.append(task)
        task.add_done_callback(finished)
        start = await queue.get()
        if start["type"] == "app.done":
            error = start["error"]
            if error is not None:
                raise error
            raise RuntimeError("the ASGI app produced no response")
        assert start["type"] == "http.response.start"
        return httpx2.Response(
            start["status"],
            headers=start.get("headers", []),
            stream=_StreamingBodyStream(queue, task, self),
        )


@dataclass(frozen=True)
class EventFrame:
    id: str
    event: str
    data: dict[str, Any]


def _parse_frame(raw: str) -> EventFrame | None:
    entry_id = ""
    event = "message"
    data_lines: list[str] = []
    for line in raw.split("\n"):
        if not line or line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "id":
            entry_id = value
        elif name == "event":
            event = value
        elif name == "data":
            data_lines.append(value)
    if not data_lines and not entry_id:
        return None
    payload = cast(dict[str, Any], json.loads("\n".join(data_lines))) if data_lines else {}
    return EventFrame(id=entry_id, event=event, data=payload)


class ApiEventsHarness(ApiHarness):
    def __init__(self, settings: Settings, *, keepalive_seconds: float = KEEPALIVE_SECONDS) -> None:
        settings.api.owner_token = SecretStr(OWNER_TOKEN)
        settings.api.admin_token = SecretStr(ADMIN_TOKEN)
        settings.identity.owner_id = SessionId("demo_owner")
        settings.identity.twin_id = SessionId("demo_twin")
        self.settings = settings
        self.seeded = SeededTransport()
        self.transport = self.seeded
        self.store = SessionStore(settings.paths.record_dir)
        self.status = MemorySessionStatus()
        self.app = create_app(
            settings,
            ApiDeps(
                transport=lambda: self.seeded,
                snapshot_store=lambda: self.store,
                session_status=lambda: self.status,
                now_iso=lambda: AT,
                event_keepalive_seconds=keepalive_seconds,
            ),
        )
        self.client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url=BASE_URL
        )
        self.stream_transport = _StreamingASGITransport(self.app)
        self.stream_client = httpx2.AsyncClient(transport=self.stream_transport, base_url=BASE_URL)

    async def seed_events(
        self,
        meeting_id: str,
        entries: list[tuple[str, str, dict[str, str]]],
        *,
        phase: PublicPhase = "created",
        terminal_reason: TerminalReason | None = None,
    ) -> None:
        if self.store.load(meeting_id) is None:
            create = SessionCreate.model_validate({**SESSION_PAYLOAD, "meeting_id": meeting_id})
            self.store.save_new(build_snapshot(create, meeting_id=meeting_id, at=AT))
            self.status.create(meeting_id, create.owner_id, STATUS_AT)
        if phase != "created" or terminal_reason is not None:
            self.status.record_phase(meeting_id, phase, STATUS_AT, terminal_reason=terminal_reason)
        self.seeded.seed_events(meeting_id, entries)

    def seed_raw_events(self, meeting_id: str, entries: list[tuple[str, dict[str, str]]]) -> None:
        self.seeded.seed_raw_events(meeting_id, entries)

    def _event_path(self, meeting_id: str) -> str:
        return f"/v1/sessions/{meeting_id}/events"

    def _event_headers(self, after: str | None) -> dict[str, str]:
        headers = self._headers("owner", None)
        if after is not None:
            headers["Last-Event-Id"] = after
        return headers

    async def stream_events(
        self, meeting_id: str, *, after: str | None = None
    ) -> AsyncGenerator[EventFrame, None]:
        async with self.stream_client.stream(
            "GET", self._event_path(meeting_id), headers=self._event_headers(after)
        ) as response:
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n\n" in buffer:
                    raw, buffer = buffer.split("\n\n", 1)
                    frame = _parse_frame(raw)
                    if frame is not None:
                        yield frame

    async def stream_lines(self, meeting_id: str) -> AsyncGenerator[str, None]:
        async with self.stream_client.stream(
            "GET", self._event_path(meeting_id), headers=self._event_headers(None)
        ) as response:
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    yield line

    async def get_events_response(
        self, meeting_id: str, *, after: str | None = None
    ) -> httpx2.Response:
        headers: Mapping[str, str] | None = {"Last-Event-Id": after} if after is not None else None
        return await self.get(self._event_path(meeting_id), headers=headers)

    @override
    async def aclose(self) -> None:
        await self.stream_client.aclose()
        await super().aclose()


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[ApiEventsHarness]:
    harness = ApiEventsHarness(settings)
    try:
        yield harness
    finally:
        await harness.aclose()


async def test_events_replay_in_order_and_close_on_terminal(api: ApiEventsHarness) -> None:
    await api.seed_events(
        "m-1",
        [
            ("5-0", "phase_changed", {"phase": "joining"}),
            ("6-0", "phase_changed", {"phase": "active"}),
            ("7-0", "session_ended", {"phase": "ended", "terminal_reason": "left_by_owner"}),
        ],
    )
    frames = [frame async for frame in api.stream_events("m-1")]
    assert [frame.id for frame in frames] == ["5-0", "6-0", "7-0"]
    assert frames[-1].event == "session_ended"
    assert all(frame.data["v"] == "1" for frame in frames)


async def test_last_event_id_resumes_after_the_cursor(api: ApiEventsHarness) -> None:
    await api.seed_events(
        "m-1",
        [
            ("5-0", "phase_changed", {"phase": "joining"}),
            ("6-0", "session_ended", {"phase": "ended", "terminal_reason": "left_by_owner"}),
        ],
    )
    frames = [frame async for frame in api.stream_events("m-1", after="5-0")]
    assert [frame.id for frame in frames] == ["6-0"]


async def test_expired_cursor_is_named_not_a_silent_empty_stream(api: ApiEventsHarness) -> None:
    await api.seed_events("m-1", [("50-0", "phase_changed", {"phase": "active"})])
    response = await api.get_events_response("m-1", after="5-0")
    assert response.status_code == 410
    assert response.json()["error"]["details"] == {"reason": "cursor_expired"}


async def test_malformed_cursor_is_rejected(api: ApiEventsHarness) -> None:
    await api.seed_events("m-1", [("5-0", "phase_changed", {"phase": "active"})])
    response = await api.get_events_response("m-1", after="not-an-entry-id")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "input.invalid"


async def test_smuggled_evidence_stops_the_stream_with_a_named_fault(
    api: ApiEventsHarness,
) -> None:
    await api.seed_events("m-1", [])
    api.seed_raw_events(
        "m-1",
        [
            (
                "9-0",
                {
                    "v": "1",
                    "type": "phase_changed",
                    "at": AT,
                    "meeting_id": "m-1",
                    "phase": "active",
                    "evidence": "smith-marker",
                },
            )
        ],
    )
    frames = [frame async for frame in api.stream_events("m-1")]
    assert [frame.event for frame in frames] == ["fault"]
    assert frames[0].id == "9-0"
    assert frames[0].data["type"] == "fault"
    assert "evidence" in frames[0].data["reason"]
    assert "smith-marker" not in json.dumps([frame.data for frame in frames])


async def test_terminal_snapshot_synthesizes_one_session_ended(api: ApiEventsHarness) -> None:
    meeting_id = await api.create_terminal_session(phase="ended", terminal_reason="left_by_owner")
    frames = [frame async for frame in api.stream_events(meeting_id)]
    assert len(frames) == 1
    assert frames[0].event == "session_ended"
    assert frames[0].data["phase"] == "ended"
    assert frames[0].data["terminal_reason"] == "left_by_owner"


async def test_keepalive_comment_when_the_stream_is_idle(api: ApiEventsHarness) -> None:
    await api.seed_events("m-1", [])
    seen = ""
    stream = api.stream_lines("m-1")
    try:
        async for line in stream:
            if line == ": keepalive":
                seen = line
                break
    finally:
        await stream.aclose()
    assert seen == ": keepalive"


async def test_client_cancel_finishes_without_leaked_tasks(api: ApiEventsHarness) -> None:
    await api.seed_events("m-1", [])
    baseline = {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}
    stream = api.stream_lines("m-1")
    first = await anext(stream)
    assert first == ": keepalive"
    await stream.aclose()
    pending = baseline
    for _ in range(SETTLE_STEPS):
        await asyncio.sleep(SETTLE_SECONDS)
        pending = {
            task for task in asyncio.all_tasks() if task is not asyncio.current_task()
        } - baseline
        if not pending:
            break
    assert pending == set()
    assert api.stream_transport.tasks
    assert all(task.done() for task in api.stream_transport.tasks)
