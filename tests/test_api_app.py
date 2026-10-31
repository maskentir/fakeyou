from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import httpx2
import pytest
from fastapi import Request
from pydantic import SecretStr

from fakeyou.api.app import create_app
from fakeyou.api.status import session_status
from fakeyou.api.transport import shared_stream_transport
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.db.session_status import SessionStatusStore
from fakeyou.transport.streams import StreamTransport


def _client(settings: Settings) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_app(settings)),
        base_url="http://testserver",
    )


def _boom() -> None:
    raise RuntimeError("sekret-uji")


async def test_health_and_openapi_are_generated(settings: Settings) -> None:
    async with _client(settings) as client:
        assert (await client.get("/v1/health")).json() == {"status": "ok"}
        document = (await client.get("/openapi.json")).json()
    assert document["openapi"].startswith("3.1")
    assert all(path.startswith("/v1") for path in document["paths"])


async def test_framework_errors_use_the_single_envelope(settings: Settings) -> None:
    async with _client(settings) as client:
        missing = await client.get("/v1/nope")
        wrong_method = await client.post("/v1/health")
    for response in (missing, wrong_method):
        payload = response.json()
        assert set(payload) == {"error"}
        assert set(payload["error"]) <= {"code", "message", "details"}
        assert payload["error"]["code"]
    assert missing.status_code == 404
    assert wrong_method.status_code == 405


async def test_unhandled_errors_are_internal_failure_envelopes(settings: Settings) -> None:
    app = create_app(settings)
    app.add_api_route("/v1/boom", _boom)
    transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/v1/boom")
    assert response.status_code == 500
    payload = response.json()
    assert set(payload) == {"error"}
    assert payload["error"]["code"] == "internal.failure"
    assert "sekret-uji" not in response.text
    assert "Traceback" not in response.text


async def test_one_stream_transport_serves_every_request_and_closes_at_shutdown(
    settings: Settings,
) -> None:
    settings.transport.redis.url = SecretStr("redis://127.0.0.1:6379/0")
    app = create_app(settings)
    request = cast(Request, SimpleNamespace(app=app))
    closed: list[bool] = []

    async def close(_transport: StreamTransport) -> None:
        closed.append(True)

    with patch.object(StreamTransport, "aclose", close):
        async with app.router.lifespan_context(app):
            first = shared_stream_transport(request)
            second = shared_stream_transport(request)
            assert isinstance(first, StreamTransport)
            assert first is second
    assert closed == [True]


async def test_the_session_status_store_lives_for_the_app_and_closes_at_shutdown(
    settings: Settings,
) -> None:
    app = create_app(settings)
    request = cast(Request, SimpleNamespace(app=app))
    closed: list[bool] = []

    async def close(_store: SessionStatusStore) -> None:
        closed.append(True)

    async def current(_store: SessionStatusStore) -> None:
        return None

    with (
        patch.object(SessionStatusStore, "close", close),
        patch.object(SessionStatusStore, "require_current", current),
    ):
        async with app.router.lifespan_context(app):
            first = await session_status(request)
            second = await session_status(request)
            assert isinstance(first, SessionStatusStore)
            assert first is second
    assert closed == [True]


async def test_the_api_refuses_to_start_without_a_database(settings: Settings) -> None:
    settings.database.url = None
    app = create_app(settings)

    with pytest.raises(FakeYouError, match="DATABASE__URL") as caught:
        async with app.router.lifespan_context(app):
            pass
    assert caught.value.code is ErrorCode.DATABASE_URL_MISSING
