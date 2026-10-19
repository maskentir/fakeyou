import argparse
import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx2

ROOT = Path(__file__).resolve().parents[2]
PERSONA_PATH = ROOT / "examples" / "persona.example.json"
BRIEF_PATH = ROOT / "examples" / "meeting-brief.example.json"
TOKEN_VARIABLE = "FAKEYOU_API_TOKEN"
REPLY_KEY_VARIABLE = "FAKEYOU_EXAMPLE_REPLY_KEY"
REPLY_KEY_HEADER = "X-Fakeyou-Key-Reply"
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
REQUEST_TIMEOUT_SECONDS = 30.0
POLL_ATTEMPTS = 60
POLL_INTERVAL_SECONDS = 1.0
TERMINAL_EVENT = "session_ended"


class ExampleError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{message} [{code}]")
        self.code = code


def _envelope_error(response: httpx2.Response) -> ExampleError:
    try:
        error = response.json()["error"]
        return ExampleError(str(error["code"]), str(error["message"]))
    except (KeyError, TypeError, ValueError):
        return ExampleError("internal.failure", f"api returned HTTP {response.status_code}")


async def _frames(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    frame_id: str | None = None
    event: str | None = None
    data: list[str] = []
    async for line in lines:
        if line == "":
            if data:
                yield {"id": frame_id, "event": event, "data": json.loads("\n".join(data))}
            frame_id, event, data = None, None, []
        elif line.startswith("id:"):
            frame_id = line[len("id:") :].strip()
        elif line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data.append(line[len("data:") :].lstrip(" "))


class Api:
    def __init__(self, *, base_url: str, token: str, transport: httpx2.AsyncBaseTransport | None):
        self._http = httpx2.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,
        )

    async def __aenter__(self) -> "Api":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, json=body, headers=headers)
        except httpx2.TransportError as error:
            raise ExampleError("api.unreachable", type(error).__name__) from error
        if response.status_code >= 400:
            raise _envelope_error(response)
        document: dict[str, Any] = response.json()
        return document

    async def events(
        self, meeting_id: str, *, last_event_id: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        headers = {"Last-Event-Id": last_event_id} if last_event_id is not None else None
        try:
            async with self._http.stream(
                "GET", f"/v1/sessions/{meeting_id}/events", headers=headers
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _envelope_error(response)
                async for frame in _frames(response.aiter_lines()):
                    yield frame
        except httpx2.TransportError as error:
            raise ExampleError("api.unreachable", type(error).__name__) from error


async def _follow_until_terminal(api: Api, meeting_id: str) -> list[dict[str, Any]]:
    received: list[dict[str, Any]] = []
    first_connection = api.events(meeting_id)
    try:
        received.append(await anext(first_connection))
    except StopAsyncIteration:
        raise ExampleError("api.stream_closed", "event stream closed before any event") from None
    finally:
        await first_connection.aclose()
    if received[-1]["data"].get("type") != TERMINAL_EVENT:
        async for frame in api.events(meeting_id, last_event_id=received[-1]["id"]):
            received.append(frame)
            if frame["data"].get("type") == TERMINAL_EVENT:
                break
    if received[-1]["data"].get("type") != TERMINAL_EVENT:
        raise ExampleError("api.stream_closed", "event stream closed before the session ended")
    return received


async def _poll_ingestion(api: Api, meeting_id: str) -> dict[str, Any]:
    view = await api.request("GET", f"/v1/sessions/{meeting_id}/ingestion")
    for _ in range(POLL_ATTEMPTS - 1):
        if view["completed"]:
            break
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        view = await api.request("GET", f"/v1/sessions/{meeting_id}/ingestion")
    return view


def _persona_and_brief(persona_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    persona: dict[str, Any] = json.loads(persona_path.read_text(encoding="utf-8"))
    brief: dict[str, Any] = json.loads(BRIEF_PATH.read_text(encoding="utf-8"))
    brief.pop("meeting_id", None)
    return persona, brief


async def run(
    *,
    base_url: str,
    link: str,
    token: str,
    transport: httpx2.AsyncBaseTransport | None = None,
    persona_path: Path = PERSONA_PATH,
) -> dict[str, Any]:
    persona, brief = _persona_and_brief(persona_path)
    reply_key = os.environ.get(REPLY_KEY_VARIABLE)
    headers = {REPLY_KEY_HEADER: reply_key} if reply_key else None
    steps: list[str] = []
    async with Api(base_url=base_url, token=token, transport=transport) as api:
        created = await api.request(
            "POST",
            "/v1/sessions",
            body={
                "link": link,
                "meeting_id": uuid4().hex,
                "owner_id": persona["owner_id"],
                "twin_id": persona["twin_id"],
                "audio": True,
                "conversation": True,
                "persona": persona,
                "brief": brief,
            },
            headers=headers,
        )
        meeting_id: str = created["meeting_id"]
        steps.append("create")

        await api.request("POST", f"/v1/sessions/{meeting_id}/join")
        steps.append("join")

        received = await _follow_until_terminal(api, meeting_id)
        steps.append("events")
        terminal = received[-1]["data"]

        stopped = await api.request(
            "POST", f"/v1/sessions/{meeting_id}/stop", body={"reason": "done"}
        )
        steps.append("stop")

        await api.request("POST", f"/v1/sessions/{meeting_id}/ingestions", body={"kind": "ingest"})
        ingestion = await _poll_ingestion(api, meeting_id)
        steps.append("ingest")

        await api.request("POST", f"/v1/sessions/{meeting_id}/debriefs", body={"force": False})
        debrief = await api.request("GET", f"/v1/sessions/{meeting_id}/debrief")
        steps.append("debrief")

        report = await api.request("GET", f"/v1/sessions/{meeting_id}/report")
        steps.append("report")

    return {
        "steps": steps,
        "meeting_id": meeting_id,
        "phase": stopped["phase"],
        "terminal_reason": terminal.get("terminal_reason"),
        "events_received": len(received),
        "ingestion": ingestion,
        "debrief": {
            "status": debrief["status"],
            "review_status": debrief["review_status"],
            "cached": debrief["cached"],
        },
        "report": report,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reference BYOF client for the fakeyou API")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--link", required=True)
    parser.add_argument("--persona", type=Path, default=PERSONA_PATH)
    arguments = parser.parse_args(argv)
    token = os.environ.get(TOKEN_VARIABLE)
    if not token:
        sys.stderr.write(f"byof-client: error: {TOKEN_VARIABLE} is not set\n")
        return 2
    try:
        output = asyncio.run(
            run(
                base_url=arguments.base_url,
                link=arguments.link,
                token=token,
                persona_path=arguments.persona,
            )
        )
    except ExampleError as error:
        sys.stderr.write(f"byof-client: error: {error}\n")
        return 1
    sys.stdout.write(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
