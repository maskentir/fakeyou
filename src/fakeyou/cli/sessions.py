import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

from fakeyou.catalog.errors import ErrorCode
from fakeyou.catalog.exit_codes import TERMINAL_EXIT_CODES, ExitCode
from fakeyou.cli.client import ApiClient, ClientSettings, api_failure, owner_client
from fakeyou.cli.output import emit_json
from fakeyou.config.meeting_request import MeetingRequest, validate_meeting_request

WORKER_STOP_POLL_SECONDS = 2.0
WORKER_STOP_LIMIT_SECONDS = 60.0
TERMINAL_PHASES = frozenset({"ended", "failed"})


@dataclass(frozen=True, slots=True)
class SessionOptions:
    link: str
    meeting_id: str | None = None
    persona_path: Path | None = None
    brief_path: Path | None = None
    owner_id: str | None = None
    twin_id: str | None = None
    twin_name: str | None = None
    lobby_timeout: float | None = None
    duration: float | None = None
    headless: bool | None = None
    audio: bool = False
    initial_speech: str | None = None
    conversation: bool = False
    memory: bool = False
    wake_phrase: str | None = None
    barge_in: bool = False


def _read_json(path: Path, unreadable: ErrorCode) -> dict[str, Any]:
    try:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise api_failure(unreadable, reason=type(error).__name__) from None
    return document


def _brief_document(path: Path, meeting_id: str | None) -> dict[str, Any]:
    document = _read_json(path, ErrorCode.MEETING_BRIEF_UNREADABLE)
    if "meeting_id" in document:
        if document.pop("meeting_id") != meeting_id:
            raise api_failure(ErrorCode.MEETING_BRIEF_SCOPE_MISMATCH)
    return document


def session_body(options: SessionOptions) -> dict[str, Any]:
    validate_meeting_request(
        MeetingRequest(
            link=options.link,
            lobby_timeout=options.lobby_timeout,
            duration=options.duration,
            initial_speech=options.initial_speech,
            audio=options.audio,
            conversation=options.conversation,
            memory=options.memory,
            persona_path=options.persona_path,
            brief_path=options.brief_path,
            meeting_id=options.meeting_id,
            wake_phrase=options.wake_phrase,
            barge_in=options.barge_in,
            owner_id=options.owner_id,
            twin_id=options.twin_id,
        )
    )
    persona = (
        _read_json(options.persona_path, ErrorCode.MEMORY_PERSONA_UNREADABLE)
        if options.persona_path is not None
        else None
    )
    brief = (
        _brief_document(options.brief_path, options.meeting_id)
        if options.brief_path is not None
        else None
    )
    owner_id = options.owner_id or (persona or {}).get("owner_id")
    twin_id = options.twin_id or (persona or {}).get("twin_id")
    if owner_id is None or twin_id is None:
        raise api_failure(ErrorCode.PERSONA_IDENTITY_MISMATCH)
    return {
        "link": options.link,
        "meeting_id": options.meeting_id,
        "owner_id": owner_id,
        "twin_id": twin_id,
        "twin_name": options.twin_name,
        "lobby_timeout": options.lobby_timeout,
        "duration": options.duration,
        "headless": options.headless,
        "audio": options.audio,
        "initial_speech": options.initial_speech,
        "conversation": options.conversation,
        "persona": persona,
        "brief": brief,
        "memory": options.memory,
        "wake_phrase": options.wake_phrase,
        "barge_in": options.barge_in,
    }


def _terminal_exit(terminal: dict[str, Any]) -> ExitCode:
    if terminal.get("phase") == "failed" and "terminal_reason" not in terminal:
        return ExitCode.FAILURE
    reason = terminal.get("terminal_reason")
    if reason is None:
        return ExitCode.SUCCESS
    return TERMINAL_EXIT_CODES.get(reason, ExitCode.FAILURE)


async def _follow_to_terminal(client: ApiClient, meeting_id: str) -> dict[str, Any]:
    async for event in client.follow_events(meeting_id):
        if event["type"] == "session_ended":
            return event
    raise api_failure(ErrorCode.API_STREAM_CLOSED, meeting_id=meeting_id)


async def run_session(
    options: SessionOptions,
    *,
    detach: bool,
    settings: ClientSettings,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    body = session_body(options)
    async with owner_client(settings, transport=transport) as client:
        created = await client.create_session(body)
        meeting_id = created["meeting_id"]
        started = await client.join_session(meeting_id)
        if detach:
            emit_json(started)
            return
        terminal = await _follow_to_terminal(client, meeting_id)
    emit_json(terminal)
    exit_code = _terminal_exit(terminal)
    if exit_code != ExitCode.SUCCESS:
        raise SystemExit(exit_code)


async def resume_session(
    meeting_id: str,
    *,
    detach: bool,
    settings: ClientSettings,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    async with owner_client(settings, transport=transport) as client:
        resumed = await client.resume_session(meeting_id)
        if detach:
            emit_json(resumed)
            return
        terminal = await _follow_to_terminal(client, meeting_id)
    emit_json(terminal)
    exit_code = _terminal_exit(terminal)
    if exit_code != ExitCode.SUCCESS:
        raise SystemExit(exit_code)


async def worker_status(
    *,
    meeting_id: str | None,
    settings: ClientSettings,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    async with owner_client(settings, transport=transport) as client:
        if meeting_id is None:
            emit_json(await client.list_sessions())
            return
        emit_json(await client.get_session(meeting_id))


async def worker_stop(
    *,
    meeting_id: str,
    settings: ClientSettings,
    limit_seconds: float = WORKER_STOP_LIMIT_SECONDS,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_seconds
    async with owner_client(settings, transport=transport) as client:
        await client.stop_session(meeting_id)
        while True:
            view = await client.get_session(meeting_id)
            if view["phase"] in TERMINAL_PHASES:
                break
            if loop.time() >= deadline:
                raise api_failure(
                    ErrorCode.API_JOB_TIMEOUT,
                    kind="stop",
                    job_id=meeting_id,
                    seconds=int(limit_seconds),
                )
            await asyncio.sleep(WORKER_STOP_POLL_SECONDS)
    emit_json(view)
