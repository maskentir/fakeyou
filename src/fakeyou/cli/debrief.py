import asyncio
from typing import Any

import httpx2

from fakeyou.catalog.errors import ErrorCode
from fakeyou.cli.client import ClientSettings, api_failure, owner_client
from fakeyou.cli.output import emit_json

DEBRIEF_POLL_INTERVAL_SECONDS = 2.0
DEBRIEF_TIMEOUT_SECONDS = 1800.0


async def debrief_meeting(
    *,
    meeting_id: str,
    force: bool,
    settings: ClientSettings,
    limit_seconds: float = DEBRIEF_TIMEOUT_SECONDS,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    async with owner_client(settings, transport=transport) as client:
        await client.submit_debrief(meeting_id, force=force)
        view = await _wait_for_available(
            lambda: client.get_debrief(meeting_id),
            meeting_id=meeting_id,
            limit_seconds=limit_seconds,
        )
    emit_json(
        {
            "meeting_id": meeting_id,
            "status": "completed",
            "review_status": view["review_status"],
            "cached": view["cached"],
        }
    )


async def _wait_for_available(
    fetch: Any,
    *,
    meeting_id: str,
    limit_seconds: float,
) -> dict[str, Any]:
    interval = DEBRIEF_POLL_INTERVAL_SECONDS
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_seconds
    while True:
        view: dict[str, Any] = await fetch()
        if view["status"] == "available":
            return view
        if loop.time() >= deadline:
            raise api_failure(
                ErrorCode.API_JOB_TIMEOUT,
                kind="debrief",
                job_id=meeting_id,
                seconds=int(limit_seconds),
            )
        await asyncio.sleep(interval)
