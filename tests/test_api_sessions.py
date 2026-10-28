import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from pydantic import SecretStr

from fakeyou.api.app import ApiDeps, create_app
from fakeyou.api.schemas import SessionCreate, build_snapshot
from fakeyou.api.sessions import SessionStore
from fakeyou.config.identifiers import SessionId, is_identifier
from fakeyou.config.settings import Settings
from fakeyou.foundation.taxonomy import PublicPhase, TerminalReason
from fakeyou.transport.schemas import command_stream
from tests._session_status_fakes import MemorySessionStatus

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
BASE_URL = "http://testserver"
AT = "2026-10-03T12:00:00+00:00"
OWNER_TOKEN = "owner-secret"
ADMIN_TOKEN = "admin-secret"


def _example_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    persona = json.loads((EXAMPLES / "persona.example.json").read_text(encoding="utf-8"))
    brief = json.loads((EXAMPLES / "meeting-brief.example.json").read_text(encoding="utf-8"))
    brief.pop("meeting_id", None)
    return persona, brief


def _session_payload() -> dict[str, Any]:
    persona, brief = _example_documents()
    return {
        "link": "https://meet.google.com/abc-defg-hij",
        "meeting_id": "brief-demo-1003",
        "owner_id": "demo_owner",
        "twin_id": "demo_twin",
        "audio": True,
        "conversation": True,
        "persona": persona,
        "brief": brief,
    }


SESSION_PAYLOAD: dict[str, Any] = _session_payload()
STATUS_AT = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


class RecordingTransport:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []

    async def publish_command(self, meeting_id: str, fields: Mapping[str, str]) -> str:
        self.commands.append((command_stream(meeting_id), str(fields["type"])))
        return f"{len(self.commands)}-0"


class ApiHarness:
    def __init__(self, settings: Settings) -> None:
        settings.api.owner_token = SecretStr(OWNER_TOKEN)
        settings.api.admin_token = SecretStr(ADMIN_TOKEN)
        settings.identity.owner_id = SessionId("demo_owner")
        settings.identity.twin_id = SessionId("demo_twin")
        self.settings = settings
        self.transport = RecordingTransport()
        self.store = SessionStore(settings.paths.record_dir)
        self.status = MemorySessionStatus()
        self.app = create_app(
            settings,
            ApiDeps(
                transport=lambda: self.transport,
                snapshot_store=lambda: self.store,
                session_status=lambda: self.status,
                now_iso=lambda: AT,
            ),
        )
        self.client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url=BASE_URL
        )

    @property
    def base_url(self) -> str:
        return BASE_URL

    def _headers(self, principal: str | None, headers: Mapping[str, str] | None) -> dict[str, str]:
        resolved = dict(headers or {})
        if any(name.lower() == "authorization" for name in resolved):
            return resolved
        token = {"owner": OWNER_TOKEN, "admin": ADMIN_TOKEN}.get(principal or "")
        if token is not None:
            resolved["Authorization"] = f"Bearer {token}"
        return resolved

    async def get(
        self,
        path: str,
        *,
        principal: str | None = "owner",
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        return await self.client.get(path, headers=self._headers(principal, headers))

    async def post(
        self,
        path: str,
        *,
        principal: str | None = "owner",
        json: object = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        return await self.client.post(path, json=json, headers=self._headers(principal, headers))

    async def create_terminal_session(
        self,
        *,
        phase: PublicPhase = "ended",
        terminal_reason: TerminalReason = "stopped_by_owner",
        meeting_id: str | None = None,
    ) -> str:
        session_id = meeting_id if meeting_id is not None else uuid4().hex
        payload = {**SESSION_PAYLOAD, "meeting_id": session_id}
        create = SessionCreate.model_validate(payload)
        self.store.save_new(build_snapshot(create, meeting_id=session_id, at=AT))
        self.status.create(session_id, create.owner_id, STATUS_AT)
        self.status.record_phase(session_id, phase, STATUS_AT, terminal_reason=terminal_reason)
        return session_id

    async def aclose(self) -> None:
        await self.client.aclose()


@pytest.fixture
async def api(settings: Settings) -> AsyncIterator[ApiHarness]:
    harness = ApiHarness(settings)
    try:
        yield harness
    finally:
        await harness.aclose()


def _seed_foreign_session(api: ApiHarness, meeting_id: str) -> None:
    create = SessionCreate.model_validate(
        {
            "link": "https://meet.google.com/abc-defg-hij",
            "meeting_id": meeting_id,
            "owner_id": "other_owner",
            "twin_id": "other_twin",
        }
    )
    api.store.save_new(build_snapshot(create, meeting_id=meeting_id, at=AT))
    api.status.create(meeting_id, "other_owner", STATUS_AT)


def _property_names(node: object) -> set[str]:
    names: set[str] = set()
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            names.update(properties)
        for value in node.values():
            names |= _property_names(value)
    elif isinstance(node, list):
        for value in node:
            names |= _property_names(value)
    return names


async def test_create_freezes_snapshot_and_join_publishes_command(api: ApiHarness) -> None:
    created = await api.post("/v1/sessions", json=SESSION_PAYLOAD)
    assert created.status_code == 201
    body = created.json()
    assert body["phase"] == "created"
    assert len(body["snapshot_sha256"]) == 64
    assert "persona" not in body
    assert "path" not in created.text
    stored = api.store.load(body["meeting_id"])
    assert stored is not None
    assert stored.snapshot_sha256 == body["snapshot_sha256"]
    assert (await api.post(f"/v1/sessions/{body['meeting_id']}/join")).status_code == 202
    assert api.transport.commands == [
        ("fakeyou:meeting:" + body["meeting_id"] + ":commands", "join")
    ]


async def test_create_rejects_principal_and_document_mismatch(api: ApiHarness) -> None:
    wrong_owner = {**SESSION_PAYLOAD, "owner_id": "other_owner"}
    response = await api.post("/v1/sessions", json=wrong_owner)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "persona.identity_mismatch"
    assert api.transport.commands == []


async def test_stop_is_idempotent_after_terminal_and_hidden_cross_owner(api: ApiHarness) -> None:
    meeting_id = await api.create_terminal_session()
    stopped = await api.post(f"/v1/sessions/{meeting_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["terminal_reason"] == "stopped_by_owner"
    assert (
        await api.get(f"/v1/sessions/{meeting_id}", headers={"Authorization": "Bearer [REDACTED]"})
    ).status_code == 401


async def test_document_identity_mismatches_are_named_catalog_errors(api: ApiHarness) -> None:
    persona, brief = _example_documents()
    broken_persona = {**SESSION_PAYLOAD, "persona": {**persona, "twin_id": "other_twin"}}
    response = await api.post("/v1/sessions", json=broken_persona)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "persona.identity_mismatch"
    broken_brief = {**SESSION_PAYLOAD, "brief": {**brief, "owner_id": "other_owner"}}
    response = await api.post("/v1/sessions", json=broken_brief)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "meeting.brief_scope_mismatch"
    assert api.transport.commands == []


async def test_resume_on_created_session_is_a_conflict(api: ApiHarness) -> None:
    body = (await api.post("/v1/sessions", json=SESSION_PAYLOAD)).json()
    response = await api.post(f"/v1/sessions/{body['meeting_id']}/resume")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "meeting.unfinished_session"
    assert api.transport.commands == []


async def test_join_twice_and_join_after_terminal_are_named_conflicts(api: ApiHarness) -> None:
    body = (await api.post("/v1/sessions", json=SESSION_PAYLOAD)).json()
    meeting_id = body["meeting_id"]
    assert (await api.post(f"/v1/sessions/{meeting_id}/join")).status_code == 202
    again = await api.post(f"/v1/sessions/{meeting_id}/join")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "meeting.unfinished_session"
    terminal_id = await api.create_terminal_session()
    ended = await api.post(f"/v1/sessions/{terminal_id}/join")
    assert ended.status_code == 409
    assert ended.json()["error"]["code"] == "meeting.already_ended"


async def test_resume_and_stop_publish_exactly_their_commands(api: ApiHarness) -> None:
    meeting_id = (await api.post("/v1/sessions", json=SESSION_PAYLOAD)).json()["meeting_id"]
    await api.post(f"/v1/sessions/{meeting_id}/join")
    resumed = await api.post(f"/v1/sessions/{meeting_id}/resume")
    stopped = await api.post(f"/v1/sessions/{meeting_id}/stop")
    assert resumed.status_code == 202
    assert stopped.status_code == 202
    stream = command_stream(meeting_id)
    assert api.transport.commands == [(stream, "join"), (stream, "resume"), (stream, "stop")]


async def test_cross_owner_snapshot_is_a_404(api: ApiHarness) -> None:
    _seed_foreign_session(api, "other-owner-1")
    response = await api.get("/v1/sessions/other-owner-1")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "api.not_found"


async def test_list_returns_only_the_principal_sessions(api: ApiHarness) -> None:
    mine = (await api.post("/v1/sessions", json=SESSION_PAYLOAD)).json()
    _seed_foreign_session(api, "other-owner-1")
    listed = (await api.get("/v1/sessions")).json()
    assert [item["meeting_id"] for item in listed] == [mine["meeting_id"]]


async def test_session_responses_carry_no_paths_or_settings(api: ApiHarness) -> None:
    created = await api.post("/v1/sessions", json=SESSION_PAYLOAD)
    body = created.json()
    assert set(body) == {
        "meeting_id",
        "phase",
        "desired_state",
        "snapshot_sha256",
        "owner_id",
        "twin_id",
        "created_at",
        "terminal_reason",
        "detail",
    }
    assert "path" not in created.text
    assert "container" not in created.text
    assert str(api.settings.paths.record_dir) not in created.text
    status = await api.get(f"/v1/sessions/{body['meeting_id']}")
    assert status.status_code == 200
    assert status.json() == body


async def test_server_issues_uuid4_hex_meeting_ids(api: ApiHarness) -> None:
    payload = {
        "link": "https://meet.google.com/abc-defg-hij",
        "owner_id": "demo_owner",
        "twin_id": "demo_twin",
    }
    body = (await api.post("/v1/sessions", json=payload)).json()
    meeting_id = body["meeting_id"]
    assert len(meeting_id) == 32
    assert is_identifier(meeting_id)
    assert all(character in "0123456789abcdef" for character in meeting_id)


async def test_openapi_covers_the_six_session_operations_without_path_fields(
    api: ApiHarness,
) -> None:
    document = (await api.get("/openapi.json")).json()
    operations = {
        (method, path)
        for path, methods in document["paths"].items()
        for method in methods
        if method in {"get", "post", "put", "delete", "patch"}
    }
    expected = {
        ("post", "/v1/sessions"),
        ("get", "/v1/sessions"),
        ("get", "/v1/sessions/{meeting_id}"),
        ("post", "/v1/sessions/{meeting_id}/join"),
        ("post", "/v1/sessions/{meeting_id}/stop"),
        ("post", "/v1/sessions/{meeting_id}/resume"),
    }
    assert expected <= operations
    fields = _property_names(document)
    assert not [name for name in fields if name.endswith("_path") or name.startswith("container_")]


async def test_the_api_records_what_the_owner_wants_and_never_the_phase(api: ApiHarness) -> None:
    meeting_id = (await api.post("/v1/sessions", json=SESSION_PAYLOAD)).json()["meeting_id"]

    joined = (await api.post(f"/v1/sessions/{meeting_id}/join")).json()
    stopped = (await api.post(f"/v1/sessions/{meeting_id}/stop")).json()

    assert (joined["desired_state"], joined["phase"]) == ("joined", "created")
    assert (stopped["desired_state"], stopped["phase"]) == ("stopped", "created")


async def test_a_finished_session_shows_why_it_ended(api: ApiHarness) -> None:
    meeting_id = (await api.post("/v1/sessions", json=SESSION_PAYLOAD)).json()["meeting_id"]
    api.status.record_phase(
        meeting_id,
        "ended",
        STATUS_AT,
        terminal_reason="left_by_participant",
        detail="twin left the call on request",
    )

    body = (await api.get(f"/v1/sessions/{meeting_id}")).json()

    assert (body["phase"], body["terminal_reason"], body["detail"]) == (
        "ended",
        "left_by_participant",
        "twin left the call on request",
    )
