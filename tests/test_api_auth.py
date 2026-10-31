import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import pytest
from pydantic import SecretStr

from fakeyou.api.app import ApiDeps, create_app
from fakeyou.api.auth import Principal, TokenStore, require_owned
from fakeyou.api.ratelimit import RateLimiter
from fakeyou.api.schemas import SessionCreate, build_snapshot
from fakeyou.api.sessions import SessionStore
from fakeyou.api.status import session_status
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from tests._sanitize_support import SYNTHETIC_MARKERS, assert_no_synthetic_markers
from tests._session_status_fakes import MemorySessionStatus

AT = "2026-10-03T12:00:00+00:00"
EPISODE = "9c8b7a6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d"
PERSONA_EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "persona.example.json"
SESSION_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("post", "/v1/sessions"),
    ("post", "/v1/sessions/m-1/join"),
    ("post", "/v1/sessions/m-1/stop"),
    ("post", "/v1/sessions/m-1/resume"),
    ("get", "/v1/sessions/m-1"),
    ("get", "/v1/sessions/m-1/events"),
    ("get", "/v1/sessions"),
)
SESSION_PATH_OPERATIONS: tuple[tuple[str, str], ...] = tuple(
    operation for operation in SESSION_OPERATIONS if operation[1] != "/v1/sessions"
)
MEMORY_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("post", "/v1/sessions/m-1/ingestions"),
    ("get", "/v1/sessions/m-1/ingestion"),
    ("post", "/v1/sessions/m-1/debriefs"),
    ("get", "/v1/sessions/m-1/debrief"),
    ("get", "/v1/sessions/m-1/report"),
    ("get", "/v1/jobs/1-0"),
)
MEMORY_SESSION_OPERATIONS: tuple[tuple[str, str], ...] = tuple(
    operation for operation in MEMORY_OPERATIONS if operation[1] != "/v1/jobs/1-0"
)
TWIN_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("post", "/v1/twins/demo_twin/notes"),
    ("post", f"/v1/twins/demo_twin/notes/{EPISODE}/corrections"),
    ("delete", f"/v1/twins/demo_twin/notes/{EPISODE}"),
)
ALL_OPERATIONS: tuple[tuple[str, str], ...] = (
    SESSION_OPERATIONS + MEMORY_OPERATIONS + TWIN_OPERATIONS
)
SESSION_BODY_POST_OPERATIONS: tuple[tuple[str, str], ...] = (("post", "/v1/sessions"),)


@asynccontextmanager
async def _client(
    settings: Settings,
    *,
    owner: str | None = None,
    admin: str | None = None,
    limit: int | None = None,
    transport: object | None = None,
) -> AsyncIterator[httpx2.AsyncClient]:
    if owner is not None:
        settings.api.owner_token = SecretStr(owner)
    if admin is not None:
        settings.api.admin_token = SecretStr(admin)
    if limit is not None:
        settings.api.rate_limit_per_minute = limit
    transport_factory = None if transport is None else (lambda: transport)
    status = MemorySessionStatus()
    app = create_app(
        settings,
        ApiDeps(test_routes=True, transport=transport_factory),
    )
    app.dependency_overrides[session_status] = lambda: status
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client


async def test_missing_wrong_and_cross_principal_tokens(settings: Settings) -> None:
    async with _client(settings, owner="owner-secret", admin="admin-secret") as client:
        assert (await client.get("/v1/test/owner")).status_code == 401
        assert (
            await client.get("/v1/test/owner", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        assert (
            await client.get("/v1/test/admin", headers={"Authorization": "Bearer owner-secret"})
        ).status_code == 403
        ok = await client.get("/v1/test/owner", headers={"Authorization": "Bearer owner-secret"})
        admin_ok = await client.get(
            "/v1/test/admin", headers={"Authorization": "Bearer admin-secret"}
        )
    assert ok.status_code == 200
    assert ok.json()["owner_id"] == settings.identity.owner_id
    assert admin_ok.status_code == 200


def test_token_store_keeps_only_hashes(settings: Settings) -> None:
    settings.api.owner_token = SecretStr("owner-secret")
    settings.api.admin_token = SecretStr("admin-secret")
    store = TokenStore.from_settings(settings)
    assert "owner-secret" not in repr(store)
    assert "admin-secret" not in repr(store)


async def test_rate_limit_returns_429_with_catalog_code(settings: Settings) -> None:
    async with _client(settings, owner="owner-secret", limit=2) as client:
        headers = {"Authorization": "Bearer owner-secret"}
        codes = [
            (await client.get("/v1/test/owner", headers=headers)).status_code for _ in range(3)
        ]
        limited = await client.get("/v1/test/owner", headers=headers)
    assert codes == [200, 200, 429]
    assert limited.json()["error"]["code"] == "api.rate_limited"


async def test_rejected_marker_token_leaks_nowhere(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    marker = SYNTHETIC_MARKERS[0]
    with caplog.at_level(logging.DEBUG):
        async with _client(settings, owner="owner-secret") as client:
            response = await client.get(
                "/v1/test/owner", headers={"Authorization": f"Bearer {marker}"}
            )
    assert response.status_code == 401
    assert_no_synthetic_markers(response.text)
    assert_no_synthetic_markers(dict(response.headers))
    assert_no_synthetic_markers(caplog.text)


async def test_test_routes_are_absent_without_the_flag(settings: Settings) -> None:
    app = create_app(settings)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/v1/test/owner")).status_code == 404


def test_authenticate_rejects_malformed_schemes_and_fails_closed(settings: Settings) -> None:
    settings.api.owner_token = SecretStr("owner-secret")
    store = TokenStore.from_settings(settings)
    for header in (None, "Bearer", "Bearer ", "Basic owner-secret", "owner-secret"):
        with pytest.raises(FakeYouError, match="missing or invalid API credentials"):
            store.authenticate(header)
    with pytest.raises(FakeYouError, match="missing or invalid API credentials"):
        store.authenticate("Bearer admin-secret")
    assert store.authenticate("Bearer owner-secret") == Principal(
        kind="owner", owner_id=settings.identity.owner_id
    )


def test_require_owned_hides_other_owners() -> None:
    owner = Principal(kind="owner", owner_id="owner-1")
    require_owned(owner, "owner-1")
    with pytest.raises(FakeYouError, match="resource not found") as caught:
        require_owned(owner, "owner-2")
    assert caught.value.code is ErrorCode.API_NOT_FOUND


def test_rate_limiter_counts_per_identity_and_window() -> None:
    limiter = RateLimiter(1)
    owner = Principal(kind="owner", owner_id="owner-1")
    admin = Principal(kind="admin", owner_id=None)
    limiter.check(owner, at=10.0)
    limiter.check(admin, at=10.0)
    with pytest.raises(FakeYouError, match="too many requests"):
        limiter.check(owner, at=69.999)
    limiter.check(owner, at=70.0)


@pytest.mark.parametrize(("method", "path"), ALL_OPERATIONS)
async def test_session_routes_reject_missing_credentials(
    settings: Settings, method: str, path: str
) -> None:
    async with _client(settings, owner="owner-secret") as client:
        response = await client.request(method.upper(), path, json={} if method == "post" else None)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "api.unauthorized"


@pytest.mark.parametrize(("method", "path"), ALL_OPERATIONS)
async def test_session_routes_reject_admin_principals(
    settings: Settings, method: str, path: str
) -> None:
    async with _client(settings, owner="owner-secret", admin="admin-secret") as client:
        response = await client.request(
            method.upper(),
            path,
            headers={"Authorization": "Bearer admin-secret"},
            json={} if method == "post" else None,
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "api.forbidden"


def _valid_body(method: str, path: str) -> object | None:
    if method != "post":
        return None
    if path.endswith("/ingestions"):
        return {"kind": "ingest"}
    if path.endswith("/debriefs"):
        return {"force": False}
    if path.endswith(("/notes", "/corrections")):
        return {"text": "Rani owns the checklist.", "note_id": EPISODE}
    return {}


@pytest.mark.parametrize(("method", "path"), SESSION_PATH_OPERATIONS + MEMORY_SESSION_OPERATIONS)
async def test_other_owner_snapshots_are_not_found(
    settings: Settings, method: str, path: str
) -> None:
    create = SessionCreate.model_validate(
        {
            "link": "https://meet.google.com/abc-defg-hij",
            "meeting_id": "m-1",
            "owner_id": "someone_else",
            "twin_id": "someone_else_twin",
        }
    )
    SessionStore(settings.paths.record_dir).save_new(
        build_snapshot(create, meeting_id="m-1", at=AT)
    )
    async with _client(settings, owner="owner-secret") as client:
        response = await client.request(
            method.upper(),
            path,
            headers={"Authorization": "Bearer owner-secret"},
            json=_valid_body(method, path),
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api.not_found"


@pytest.mark.parametrize(("method", "path"), SESSION_BODY_POST_OPERATIONS)
async def test_broken_json_bodies_are_input_invalid(
    settings: Settings, method: str, path: str
) -> None:
    async with _client(settings, owner="owner-secret") as client:
        response = await client.request(
            method.upper(),
            path,
            headers={
                "Authorization": "Bearer owner-secret",
                "Content-Type": "application/json",
            },
            content=b"{broken",
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "input.invalid"


async def test_list_sessions_rate_limit_returns_429(settings: Settings) -> None:
    async with _client(settings, owner="owner-secret", limit=1) as client:
        headers = {"Authorization": "Bearer owner-secret"}
        first = await client.get("/v1/sessions", headers=headers)
        second = await client.get("/v1/sessions", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "api.rate_limited"


class _JobEntryTransport:
    def __init__(self, entry: dict[str, str]) -> None:
        self._entry = entry

    async def read_entry(self, _stream: str, entry_id: str) -> dict[str, str] | None:
        return self._entry if entry_id == "1-0" else None


async def test_job_of_another_owner_is_not_found_through_the_auth_matrix(
    settings: Settings,
) -> None:
    create = SessionCreate.model_validate(
        {
            "link": "https://meet.google.com/abc-defg-hij",
            "meeting_id": "m-1",
            "owner_id": "someone_else",
            "twin_id": "someone_else_twin",
        }
    )
    SessionStore(settings.paths.record_dir).save_new(
        build_snapshot(create, meeting_id="m-1", at=AT)
    )
    entry = {"v": "1", "type": "ingestion_job", "at": AT, "meeting_id": "m-1", "kind": "ingest"}
    async with _client(
        settings, owner="owner-secret", transport=_JobEntryTransport(entry)
    ) as client:
        response = await client.get(
            "/v1/jobs/1-0", headers={"Authorization": "Bearer owner-secret"}
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api.not_found"


@pytest.mark.parametrize(("method", "path"), TWIN_OPERATIONS)
async def test_other_owner_twins_are_not_found(settings: Settings, method: str, path: str) -> None:
    persona = json.loads(PERSONA_EXAMPLE.read_text(encoding="utf-8"))
    persona.update({"owner_id": "someone_else", "twin_id": "someone_else_twin"})
    create = SessionCreate.model_validate(
        {
            "link": "https://meet.google.com/abc-defg-hij",
            "meeting_id": "m-1",
            "owner_id": "someone_else",
            "twin_id": "someone_else_twin",
            "audio": True,
            "conversation": True,
            "persona": persona,
        }
    )
    SessionStore(settings.paths.record_dir).save_new(
        build_snapshot(create, meeting_id="m-1", at=AT)
    )
    foreign = path.replace("demo_twin", "someone_else_twin")
    async with _client(settings, owner="owner-secret") as client:
        response = await client.request(
            method.upper(),
            foreign,
            headers={"Authorization": "Bearer owner-secret"},
            json=_valid_body(method, foreign),
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api.not_found"


ADMIN_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("post", "/v1/admin/memory-init"),
    ("get", "/v1/admin/check"),
    ("post", "/v1/admin/chat/turns"),
)


@pytest.mark.parametrize(("method", "path"), ADMIN_OPERATIONS)
async def test_admin_routes_reject_missing_credentials(
    settings: Settings, method: str, path: str
) -> None:
    async with _client(settings, owner="owner-secret", admin="admin-secret") as client:
        response = await client.request(method.upper(), path, json={} if method == "post" else None)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "api.unauthorized"


@pytest.mark.parametrize(("method", "path"), ADMIN_OPERATIONS)
async def test_admin_routes_reject_owner_principals(
    settings: Settings, method: str, path: str
) -> None:
    async with _client(settings, owner="owner-secret", admin="admin-secret") as client:
        response = await client.request(
            method.upper(),
            path,
            headers={"Authorization": "Bearer owner-secret"},
            json={} if method == "post" else None,
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "api.forbidden"
