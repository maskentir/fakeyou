import json
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx2

from fakeyou.catalog.errors import CATALOG, ErrorCode, render_error
from fakeyou.catalog.exit_codes import ExitCode

API_URL_VARIABLE = "FAKEYOU_API_URL"
OWNER_TOKEN_VARIABLE = "FAKEYOU_API_TOKEN"
ADMIN_TOKEN_VARIABLE = "FAKEYOU_API_ADMIN_TOKEN"
DEFAULT_API_URL = "http://127.0.0.1:8000"
REQUEST_TIMEOUT_SECONDS = 30.0
_KNOWN_CODES = {code.value: code for code in ErrorCode}


class ApiFailure(Exception):
    def __init__(self, message: str, *, code: str, exit_code: ExitCode) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def api_failure(code: ErrorCode, **fields: object) -> ApiFailure:
    return ApiFailure(
        render_error(code, **fields), code=str(code), exit_code=CATALOG[code].exit_code
    )


@dataclass(frozen=True, slots=True)
class ClientSettings:
    base_url: str
    owner_token: str | None
    admin_token: str | None


def client_settings_from_env(environ: Mapping[str, str] = os.environ) -> ClientSettings:
    base_url = environ.get(API_URL_VARIABLE, DEFAULT_API_URL)
    if not base_url.startswith(("http://", "https://")):
        raise _input_invalid(reason=f"{API_URL_VARIABLE} must be an http(s) URL")
    return ClientSettings(
        base_url=base_url,
        owner_token=environ.get(OWNER_TOKEN_VARIABLE) or None,
        admin_token=environ.get(ADMIN_TOKEN_VARIABLE) or None,
    )


def _input_invalid(*, reason: str) -> ApiFailure:
    return api_failure(ErrorCode.INPUT_INVALID, reason=reason)


def _failure_from(response: httpx2.Response) -> ApiFailure:
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error["message"])
    except (KeyError, TypeError, ValueError):
        return api_failure(ErrorCode.INTERNAL_FAILURE)
    known = _KNOWN_CODES.get(code)
    exit_code = CATALOG[known].exit_code if known is not None else ExitCode.FAILURE
    return ApiFailure(f"{message} [{code}]", code=code, exit_code=exit_code)


class ApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str | None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._base_url = base_url
        self._http = httpx2.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,
        )

    async def __aenter__(self) -> "ApiClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._http.aclose()

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx2.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx2.TransportError as error:
            raise api_failure(
                ErrorCode.API_UNREACHABLE,
                url=self._base_url,
                reason=type(error).__name__,
            ) from error
        if response.status_code >= 400:
            raise _failure_from(response)
        return response

    async def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        document: dict[str, Any] = (await self._send(method, path, **kwargs)).json()
        return document

    async def health(self) -> dict[str, Any]:
        return await self._json("GET", "/v1/health")

    async def memory_init(self) -> dict[str, Any]:
        return await self._json("POST", "/v1/admin/memory-init")

    async def admin_check(self) -> dict[str, Any]:
        return await self._json("GET", "/v1/admin/check")

    async def add_note(
        self, twin_id: str, *, note_id: str, text: str, source_at: str | None
    ) -> dict[str, Any]:
        body = {"text": text, "source_at": source_at, "note_id": note_id}
        return await self._json("POST", f"/v1/twins/{twin_id}/notes", json=body)

    async def correct_note(
        self, twin_id: str, episode_uuid: str, *, note_id: str, text: str, source_at: str
    ) -> dict[str, Any]:
        body = {"text": text, "source_at": source_at, "note_id": note_id}
        return await self._json(
            "POST", f"/v1/twins/{twin_id}/notes/{episode_uuid}/corrections", json=body
        )

    async def forget_episode(self, twin_id: str, episode_uuid: str) -> None:
        await self._send("DELETE", f"/v1/twins/{twin_id}/notes/{episode_uuid}")

    async def forget_meeting(self, meeting_id: str, *, keep_record: bool) -> dict[str, Any]:
        body = {"keep_record": keep_record}
        return await self._json("POST", f"/v1/sessions/{meeting_id}/forget", json=body)

    async def submit_ingestion(self, meeting_id: str, *, kind: str) -> dict[str, Any]:
        return await self._json(
            "POST", f"/v1/sessions/{meeting_id}/ingestions", json={"kind": kind}
        )

    async def ingestion_status(self, meeting_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/v1/sessions/{meeting_id}/ingestion")

    async def submit_reembed(self, twin_id: str) -> dict[str, Any]:
        return await self._json("POST", f"/v1/twins/{twin_id}/reembeddings")

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/v1/jobs/{job_id}")

    async def submit_debrief(self, meeting_id: str, *, force: bool) -> dict[str, Any]:
        return await self._json(
            "POST", f"/v1/sessions/{meeting_id}/debriefs", json={"force": force}
        )

    async def get_debrief(self, meeting_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/v1/sessions/{meeting_id}/debrief")

    async def get_report(self, meeting_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/v1/sessions/{meeting_id}/report")

    async def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._json("POST", "/v1/sessions", json=body)

    async def join_session(self, meeting_id: str) -> dict[str, Any]:
        return await self._json("POST", f"/v1/sessions/{meeting_id}/join")

    async def resume_session(self, meeting_id: str) -> dict[str, Any]:
        return await self._json("POST", f"/v1/sessions/{meeting_id}/resume")

    async def stop_session(self, meeting_id: str) -> dict[str, Any]:
        return await self._json("POST", f"/v1/sessions/{meeting_id}/stop")

    async def get_session(self, meeting_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/v1/sessions/{meeting_id}")

    async def list_sessions(self) -> list[dict[str, Any]]:
        listed: list[dict[str, Any]] = (await self._send("GET", "/v1/sessions")).json()
        return listed

    async def follow_events(self, meeting_id: str) -> AsyncIterator[dict[str, str]]:
        try:
            async with self._http.stream("GET", f"/v1/sessions/{meeting_id}/events") as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _failure_from(response)
                async for event in _parse_sse(response.aiter_lines()):
                    yield event
        except httpx2.TransportError as error:
            raise api_failure(
                ErrorCode.API_UNREACHABLE,
                url=self._base_url,
                reason=type(error).__name__,
            ) from error

    async def chat_turn(self, *, thread_id: str, message: str, memory: bool) -> str:
        body = {"thread_id": thread_id, "message": message, "memory": memory}
        reply: str = (await self._json("POST", "/v1/admin/chat/turns", json=body))["reply"]
        return reply


async def _parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, str]]:
    data: list[str] = []
    async for line in lines:
        if line == "":
            if data:
                yield json.loads("\n".join(data))
                data = []
            continue
        if line.startswith("data:"):
            data.append(line[len("data:") :].lstrip(" "))


def admin_client(
    settings: ClientSettings, *, transport: httpx2.AsyncBaseTransport | None = None
) -> ApiClient:
    if settings.admin_token is None:
        raise api_failure(ErrorCode.API_TOKEN_MISSING, variable=ADMIN_TOKEN_VARIABLE)
    return ApiClient(base_url=settings.base_url, token=settings.admin_token, transport=transport)


def owner_client(
    settings: ClientSettings, *, transport: httpx2.AsyncBaseTransport | None = None
) -> ApiClient:
    if settings.owner_token is None:
        raise api_failure(ErrorCode.API_TOKEN_MISSING, variable=OWNER_TOKEN_VARIABLE)
    return ApiClient(base_url=settings.base_url, token=settings.owner_token, transport=transport)
