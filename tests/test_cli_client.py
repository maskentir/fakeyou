import json
from collections.abc import Callable, Mapping

import httpx2
import pytest
from pydantic import SecretStr

from fakeyou.api.app import ApiDeps, create_app
from fakeyou.api.sessions import SessionStore
from fakeyou.api.status import session_status
from fakeyou.catalog.errors import ErrorCode
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.client import (
    ADMIN_TOKEN_VARIABLE,
    API_URL_VARIABLE,
    DEFAULT_API_URL,
    OWNER_TOKEN_VARIABLE,
    ApiClient,
    ApiFailure,
    ClientSettings,
    admin_client,
    client_settings_from_env,
    owner_client,
)
from fakeyou.cli.diagnostics import check_api
from fakeyou.cli.sessions import SessionOptions, session_body
from fakeyou.config.settings import Settings
from tests._session_status_fakes import MemorySessionStatus

Handler = Callable[[httpx2.Request], httpx2.Response]


def _client(handler: Handler, *, token: str | None = "admin-secret") -> ApiClient:
    return ApiClient(
        base_url="http://api.test",
        token=token,
        transport=httpx2.MockTransport(handler),
    )


def _envelope(code: str, message: str, status: int) -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"code": code, "message": message}})


def test_client_settings_default_to_the_local_bind_without_tokens() -> None:
    settings = client_settings_from_env({})

    assert settings == ClientSettings(base_url=DEFAULT_API_URL, owner_token=None, admin_token=None)


def test_client_settings_read_the_three_client_variables() -> None:
    settings = client_settings_from_env(
        {
            API_URL_VARIABLE: "http://api.internal:8000",
            OWNER_TOKEN_VARIABLE: "owner-secret",
            ADMIN_TOKEN_VARIABLE: "admin-secret",
        }
    )

    assert settings.base_url == "http://api.internal:8000"
    assert settings.owner_token == "owner-secret"
    assert settings.admin_token == "admin-secret"


def test_missing_admin_token_is_an_input_rejection_not_a_traceback() -> None:
    settings = ClientSettings(base_url="http://api.test", owner_token="o", admin_token=None)

    with pytest.raises(ApiFailure) as raised:
        admin_client(settings)

    assert raised.value.code == ErrorCode.API_TOKEN_MISSING
    assert raised.value.exit_code is ExitCode.INPUT_REJECTED
    assert "admin-secret" not in str(raised.value)


def test_missing_owner_token_is_an_input_rejection() -> None:
    settings = ClientSettings(base_url="http://api.test", owner_token=None, admin_token="a")

    with pytest.raises(ApiFailure) as raised:
        owner_client(settings)

    assert raised.value.exit_code is ExitCode.INPUT_REJECTED


async def test_memory_init_posts_with_the_admin_bearer_token() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"status": "initialized"})

    async with _client(handler) as client:
        assert await client.memory_init() == {"status": "initialized"}

    assert seen[0].method == "POST"
    assert seen[0].url.path == "/v1/admin/memory-init"
    assert seen[0].headers["authorization"] == "Bearer admin-secret"


async def test_api_error_envelope_maps_to_the_catalog_exit_code() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return _envelope("meeting.unfinished_session", "meeting has an unfinished session", 409)

    async with _client(handler) as client:
        with pytest.raises(ApiFailure) as raised:
            await client.admin_check()

    assert raised.value.code == ErrorCode.MEETING_UNFINISHED_SESSION
    assert "unfinished session" in str(raised.value)
    assert "[meeting.unfinished_session]" in str(raised.value)


async def test_unknown_error_code_falls_back_to_a_plain_failure() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return _envelope("made.up_code", "something new", 418)

    async with _client(handler) as client:
        with pytest.raises(ApiFailure) as raised:
            await client.admin_check()

    assert raised.value.exit_code is ExitCode.FAILURE


async def test_unreachable_api_becomes_a_named_infra_failure() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    async with _client(handler) as client:
        with pytest.raises(ApiFailure) as raised:
            await client.health()

    assert raised.value.code == ErrorCode.API_UNREACHABLE
    assert raised.value.exit_code is ExitCode.INFRA_FAILURE
    assert "http://api.test" in str(raised.value)


async def test_chat_turn_sends_the_frozen_body_and_returns_the_reply() -> None:
    bodies: list[object] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={"reply": "ok"})

    async with _client(handler) as client:
        reply = await client.chat_turn(thread_id="debug-1", message="hi", memory=False)

    assert reply == "ok"
    assert bodies == [{"thread_id": "debug-1", "message": "hi", "memory": False}]


async def test_check_without_admin_token_only_reports_health(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(f"{request.method} {request.url.path}")
        assert "authorization" not in request.headers
        return httpx2.Response(200, json={"status": "ok"})

    settings = ClientSettings(base_url="http://api.test", owner_token=None, admin_token=None)

    await check_api(settings, transport=httpx2.MockTransport(handler))

    assert seen == ["GET /v1/health"]
    assert '"status": "ok"' in capsys.readouterr().out


async def test_check_with_admin_token_prints_the_effective_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/health":
            return httpx2.Response(200, json={"status": "ok"})
        return httpx2.Response(200, json={"identity": {"owner_id": "demo"}})

    settings = ClientSettings(base_url="http://api.test", owner_token=None, admin_token="a")

    await check_api(settings, transport=httpx2.MockTransport(handler))

    assert seen == ["GET /v1/health", "GET /v1/admin/check"]
    assert '"owner_id": "demo"' in capsys.readouterr().out


def test_check_rejects_a_non_http_api_url() -> None:
    with pytest.raises(ApiFailure, match="must be an http") as raised:
        client_settings_from_env({API_URL_VARIABLE: "unix:///var/run/api.sock"})

    assert raised.value.exit_code is ExitCode.INPUT_REJECTED


async def test_debrief_submits_then_polls_until_the_report_is_available(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import fakeyou.cli.debrief as cli_debrief

    monkeypatch.setattr(cli_debrief, "DEBRIEF_POLL_INTERVAL_SECONDS", 0.0)
    states = iter(
        [
            {"status": "missing", "review_status": "unreviewed", "cached": False, "report": None},
            {"status": "available", "review_status": "unreviewed", "cached": False, "report": {}},
        ]
    )
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            return httpx2.Response(202, json={"meeting_id": "m1", "status": "queued"})
        return httpx2.Response(200, json=next(states))

    settings = ClientSettings(base_url="http://api.test", owner_token="o", admin_token=None)

    await cli_debrief.debrief_meeting(
        meeting_id="m1",
        force=False,
        settings=settings,
        transport=httpx2.MockTransport(handler),
    )

    assert seen == [
        "POST /v1/sessions/m1/debriefs",
        "GET /v1/sessions/m1/debrief",
        "GET /v1/sessions/m1/debrief",
    ]
    assert json.loads(capsys.readouterr().out) == {
        "meeting_id": "m1",
        "status": "completed",
        "review_status": "unreviewed",
        "cached": False,
    }


async def test_debrief_gives_up_with_a_named_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeyou.cli.debrief as cli_debrief

    monkeypatch.setattr(cli_debrief, "DEBRIEF_POLL_INTERVAL_SECONDS", 0.0)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(202, json={"meeting_id": "m1", "status": "queued"})
        return httpx2.Response(
            200,
            json={
                "status": "missing",
                "review_status": "unreviewed",
                "cached": False,
                "report": None,
            },
        )

    settings = ClientSettings(base_url="http://api.test", owner_token="o", admin_token=None)

    with pytest.raises(ApiFailure) as caught:
        await cli_debrief.debrief_meeting(
            meeting_id="m1",
            force=False,
            settings=settings,
            limit_seconds=0.0,
            transport=httpx2.MockTransport(handler),
        )

    assert caught.value.code == ErrorCode.API_JOB_TIMEOUT


class _Commands:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def publish_command(self, meeting_id: str, fields: Mapping[str, str]) -> str:
        del meeting_id
        self.sent.append(str(fields["type"]))
        return f"{len(self.sent)}-0"


async def test_the_cli_session_commands_are_accepted_by_the_real_api(settings: Settings) -> None:
    settings.api.owner_token = SecretStr("owner-secret")
    commands = _Commands()
    repository = MemorySessionStatus()
    app = create_app(
        settings,
        ApiDeps(
            transport=lambda: commands,
            snapshot_store=lambda: SessionStore(settings.paths.record_dir),
        ),
    )
    app.dependency_overrides[session_status] = lambda: repository
    body = session_body(
        SessionOptions(
            link="https://meet.google.com/abc-defg-hij",
            meeting_id="contract-1",
            owner_id=settings.identity.owner_id,
            twin_id=settings.identity.twin_id,
        )
    )

    async with ApiClient(
        base_url="http://testserver",
        token="owner-secret",
        transport=httpx2.ASGITransport(app=app),
    ) as client:
        created = await client.create_session(body)
        joined = await client.join_session("contract-1")
        stopped = await client.stop_session("contract-1")
        listed = await client.list_sessions()
        status = await client.get_session("contract-1")

    assert (created["phase"], joined["phase"], stopped["phase"]) == ("created",) * 3
    assert (created["desired_state"], joined["desired_state"], stopped["desired_state"]) == (
        "created",
        "joined",
        "stopped",
    )
    assert [session["meeting_id"] for session in listed] == ["contract-1"]
    assert status["meeting_id"] == "contract-1"
    assert commands.sent == ["join", "stop"]
