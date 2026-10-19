import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

import fakeyou.cli.sessions as cli_sessions
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.client import ApiFailure, ClientSettings
from fakeyou.cli.sessions import SessionOptions, run_session, session_body, worker_stop

LINK = "https://meet.google.com/abc-defg-hij"
SETTINGS = ClientSettings(base_url="http://api.test", owner_token="owner-secret", admin_token=None)
PERSONA = {"owner_id": "owner-1", "twin_id": "twin-1", "display_name": "Twin"}
SSE_FRAMES = (
    'event: session_started\ndata: {"type": "session_started", "meeting_id": "m-1"}\n\n'
    ": keepalive\n\n"
    'event: session_ended\ndata: {"type": "session_ended", "meeting_id": "m-1", '
    '"phase": "ended", "terminal_reason": "stopped_by_owner"}\n\n'
)


def _persona_file(tmp_path: Path) -> Path:
    path = tmp_path / "persona.json"
    path.write_text(json.dumps(PERSONA), encoding="utf-8")
    return path


def _client(handler: Any) -> httpx2.MockTransport:
    return httpx2.MockTransport(handler)


def test_session_body_takes_owner_and_twin_from_the_persona_file(tmp_path: Path) -> None:
    body = session_body(
        SessionOptions(
            link=LINK, persona_path=_persona_file(tmp_path), audio=True, conversation=True
        )
    )

    assert body["owner_id"] == "owner-1"
    assert body["twin_id"] == "twin-1"
    assert body["persona"] == PERSONA
    assert body["brief"] is None


def test_session_body_without_an_identity_is_rejected_before_any_request() -> None:
    with pytest.raises(ApiFailure) as caught:
        session_body(SessionOptions(link=LINK))

    assert caught.value.code == ErrorCode.PERSONA_IDENTITY_MISMATCH


def test_session_body_rejects_a_bad_link_before_any_request() -> None:
    with pytest.raises(FakeYouError, match="must be a meet.google.com|meet.google.com") as caught:
        session_body(SessionOptions(link="https://example.com/x", owner_id="o", twin_id="t"))

    assert caught.value.code == ErrorCode.MEETING_LINK_INVALID


async def test_detached_run_creates_then_joins_and_prints_the_accepted_view(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/sessions":
            return httpx2.Response(201, json={"meeting_id": "m-1", "phase": "created"})
        return httpx2.Response(202, json={"meeting_id": "m-1", "phase": "joining"})

    await run_session(
        SessionOptions(
            link=LINK, persona_path=_persona_file(tmp_path), audio=True, conversation=True
        ),
        detach=True,
        settings=SETTINGS,
        transport=_client(handler),
    )

    assert seen == ["POST /v1/sessions", "POST /v1/sessions/m-1/join"]
    assert json.loads(capsys.readouterr().out)["phase"] == "joining"


async def test_attached_run_follows_the_events_to_the_terminal_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/sessions":
            return httpx2.Response(201, json={"meeting_id": "m-1", "phase": "created"})
        if request.url.path.endswith("/join"):
            return httpx2.Response(202, json={"meeting_id": "m-1", "phase": "joining"})
        return httpx2.Response(
            200, content=SSE_FRAMES.encode(), headers={"content-type": "text/event-stream"}
        )

    await run_session(
        SessionOptions(
            link=LINK, persona_path=_persona_file(tmp_path), audio=True, conversation=True
        ),
        detach=False,
        settings=SETTINGS,
        transport=_client(handler),
    )

    printed = json.loads(capsys.readouterr().out)
    assert printed["type"] == "session_ended"
    assert printed["terminal_reason"] == "stopped_by_owner"


async def test_terminal_reason_maps_to_its_catalog_exit_code(tmp_path: Path) -> None:
    frames = SSE_FRAMES.replace("stopped_by_owner", "infra_failure")

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/sessions":
            return httpx2.Response(201, json={"meeting_id": "m-1", "phase": "created"})
        if request.url.path.endswith("/join"):
            return httpx2.Response(202, json={"meeting_id": "m-1", "phase": "joining"})
        return httpx2.Response(200, content=frames.encode())

    with pytest.raises(SystemExit) as exited:
        await run_session(
            SessionOptions(
                link=LINK, persona_path=_persona_file(tmp_path), audio=True, conversation=True
            ),
            detach=False,
            settings=SETTINGS,
            transport=_client(handler),
        )

    assert exited.value.code == ExitCode.INFRA_FAILURE


async def test_a_stream_that_closes_before_the_end_is_a_named_failure(tmp_path: Path) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/v1/sessions":
            return httpx2.Response(201, json={"meeting_id": "m-1", "phase": "created"})
        if request.url.path.endswith("/join"):
            return httpx2.Response(202, json={"meeting_id": "m-1", "phase": "joining"})
        return httpx2.Response(200, content=b": keepalive\n\n")

    with pytest.raises(ApiFailure) as caught:
        await run_session(
            SessionOptions(
                link=LINK, persona_path=_persona_file(tmp_path), audio=True, conversation=True
            ),
            detach=False,
            settings=SETTINGS,
            transport=_client(handler),
        )

    assert caught.value.code == ErrorCode.API_STREAM_CLOSED


async def test_resume_posts_to_the_resume_route_when_detached() -> None:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx2.Response(202, json={"meeting_id": "m-1", "phase": "joining"})

    await cli_sessions.resume_session(
        "m-1", detach=True, settings=SETTINGS, transport=_client(handler)
    )

    assert seen == ["POST /v1/sessions/m-1/resume"]


async def test_worker_stop_waits_for_the_session_to_reach_a_terminal_phase(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_sessions, "WORKER_STOP_POLL_SECONDS", 0.0)
    phases = iter(["stopping", "ended"])
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.method == "POST":
            return httpx2.Response(202, json={"meeting_id": "m-1", "phase": "stopping"})
        return httpx2.Response(200, json={"meeting_id": "m-1", "phase": next(phases)})

    await worker_stop(meeting_id="m-1", settings=SETTINGS, transport=_client(handler))

    assert seen == [
        "POST /v1/sessions/m-1/stop",
        "GET /v1/sessions/m-1",
        "GET /v1/sessions/m-1",
    ]
    assert json.loads(capsys.readouterr().out)["phase"] == "ended"


async def test_worker_stop_gives_up_with_a_named_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_sessions, "WORKER_STOP_POLL_SECONDS", 0.0)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(202, json={"meeting_id": "m-1", "phase": "stopping"})
        return httpx2.Response(200, json={"meeting_id": "m-1", "phase": "stopping"})

    with pytest.raises(ApiFailure) as caught:
        await worker_stop(
            meeting_id="m-1",
            settings=SETTINGS,
            limit_seconds=0.0,
            transport=_client(handler),
        )

    assert caught.value.code == ErrorCode.API_JOB_TIMEOUT


def _brief_file(tmp_path: Path, **extra: str) -> Path:
    brief = {
        "schema_version": "meeting_brief/v1",
        "owner_id": "owner-1",
        "twin_id": "twin-1",
        **extra,
    }
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(brief), encoding="utf-8")
    return path


def test_session_body_sends_the_brief_without_its_meeting_id(tmp_path: Path) -> None:
    body = session_body(
        SessionOptions(
            link=LINK,
            persona_path=_persona_file(tmp_path),
            audio=True,
            conversation=True,
            meeting_id="meeting-1",
            brief_path=_brief_file(tmp_path, meeting_id="meeting-1"),
        )
    )

    assert body["brief"] is not None
    assert "meeting_id" not in body["brief"]


def test_session_body_rejects_a_brief_bound_to_another_meeting(tmp_path: Path) -> None:
    with pytest.raises(ApiFailure) as caught:
        session_body(
            SessionOptions(
                link=LINK,
                persona_path=_persona_file(tmp_path),
                audio=True,
                conversation=True,
                meeting_id="meeting-1",
                brief_path=_brief_file(tmp_path, meeting_id="meeting-2"),
            )
        )

    assert caught.value.code == ErrorCode.MEETING_BRIEF_SCOPE_MISMATCH
