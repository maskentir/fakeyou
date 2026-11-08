import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest

import fakeyou.cli.memory as cli_memory
from fakeyou.catalog.errors import ErrorCode
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.client import ApiFailure, ClientSettings
from fakeyou.config.settings import Settings
from fakeyou.memory import maintenance
from fakeyou.memory.maintenance import initialize_indexes, run_reembed
from fakeyou.memory.memory import ReembedResult
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from tests._cli_memory_support import tracked_memory
from tests._cli_support import PERSONA_DATA, write_persona
from tests._memory_fakes import make_graphiti

SETTINGS = ClientSettings(base_url="http://api.test", owner_token="owner-secret", admin_token=None)
NO_WAIT = 0.0


def _route(
    responses: dict[tuple[str, str], httpx2.Response], seen: list[httpx2.Request]
) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        key = (request.method, request.url.path)
        if key not in responses:
            raise AssertionError(f"unexpected {key}")
        return responses[key]

    return httpx2.MockTransport(handler)


async def test_memory_forget_meeting_prints_the_complete_document(
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = {
        "removed_episodes": 2,
        "record_removed": True,
        "journal_files_removed": 1,
        "status_removed": True,
        "browser_profile_removed": False,
        "stages": [{"stage": "graph", "status": "removed", "detail": None}],
        "complete": True,
    }
    transport = _route(
        {("POST", "/v1/sessions/m1/forget"): httpx2.Response(200, json=document)}, []
    )

    await cli_memory.forget_meeting_memory(meeting_id="m1", settings=SETTINGS, transport=transport)

    assert json.loads(capsys.readouterr().out) == document


async def test_memory_forget_meeting_fails_when_a_stage_is_incomplete() -> None:
    document = {
        "removed_episodes": 0,
        "record_removed": False,
        "journal_files_removed": 0,
        "status_removed": False,
        "browser_profile_removed": False,
        "stages": [{"stage": "graph", "status": "failed", "detail": "graph is down"}],
        "complete": False,
    }
    transport = _route(
        {("POST", "/v1/sessions/m1/forget"): httpx2.Response(200, json=document)}, []
    )

    with pytest.raises(ApiFailure, match="graph is down") as caught:
        await cli_memory.forget_meeting_memory(
            meeting_id="m1", settings=SETTINGS, transport=transport
        )

    assert caught.value.code == ErrorCode.MEMORY_FORGET_FILES_INCOMPLETE
    assert caught.value.exit_code is not ExitCode.SUCCESS


async def test_reembed_polls_the_job_until_it_completes(capsys: pytest.CaptureFixture[str]) -> None:
    job = "1-0"
    polls = iter(
        [
            {
                "job_id": job,
                "kind": "reembed",
                "meeting_id": "",
                "status": "running",
                "completed": False,
                "succeeded": False,
            },
            {
                "job_id": job,
                "kind": "reembed",
                "meeting_id": "",
                "status": "done",
                "completed": True,
                "succeeded": True,
            },
        ]
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(
                202,
                json={
                    "job_id": job,
                    "kind": "reembed",
                    "meeting_id": "",
                    "status": "queued",
                    "completed": False,
                    "succeeded": False,
                },
            )
        return httpx2.Response(200, json=next(polls))

    await cli_memory.reembed_memory(
        twin_id="twin-1",
        settings=SETTINGS,
        interval=NO_WAIT,
        transport=httpx2.MockTransport(handler),
    )

    assert json.loads(capsys.readouterr().out)["succeeded"] is True


async def test_reembed_reports_a_failed_job_without_claiming_success() -> None:
    job = "1-0"

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(
                202,
                json={
                    "job_id": job,
                    "kind": "reembed",
                    "meeting_id": "",
                    "status": "queued",
                    "completed": False,
                    "succeeded": False,
                },
            )
        return httpx2.Response(
            200,
            json={
                "job_id": job,
                "kind": "reembed",
                "meeting_id": "",
                "status": "failed",
                "completed": True,
                "succeeded": False,
            },
        )

    with pytest.raises(ApiFailure) as caught:
        await cli_memory.reembed_memory(
            twin_id="twin-1",
            settings=SETTINGS,
            interval=NO_WAIT,
            transport=httpx2.MockTransport(handler),
        )

    assert caught.value.code == ErrorCode.API_JOB_FAILED


async def test_reembed_gives_up_after_its_time_limit() -> None:
    job = "1-0"

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.method == "POST":
            return httpx2.Response(
                202,
                json={
                    "job_id": job,
                    "kind": "reembed",
                    "meeting_id": "",
                    "status": "queued",
                    "completed": False,
                    "succeeded": False,
                },
            )
        return httpx2.Response(
            200,
            json={
                "job_id": job,
                "kind": "reembed",
                "meeting_id": "",
                "status": "running",
                "completed": False,
                "succeeded": False,
            },
        )

    with pytest.raises(ApiFailure) as caught:
        await cli_memory.reembed_memory(
            twin_id="twin-1",
            settings=SETTINGS,
            interval=NO_WAIT,
            limit_seconds=NO_WAIT,
            transport=httpx2.MockTransport(handler),
        )

    assert caught.value.code == ErrorCode.API_JOB_TIMEOUT
    assert caught.value.exit_code is ExitCode.INFRA_FAILURE


def test_twin_id_comes_from_the_persona_file_when_not_given(tmp_path: Path) -> None:
    persona = write_persona(tmp_path)

    assert cli_memory.twin_id_for(persona, None) == str(PERSONA_DATA["twin_id"])


def test_an_explicit_twin_id_wins_over_the_persona_file(tmp_path: Path) -> None:
    assert cli_memory.twin_id_for(write_persona(tmp_path), "explicit") == "explicit"


def test_a_missing_persona_file_is_an_unreadable_persona(tmp_path: Path) -> None:
    with pytest.raises(ApiFailure) as caught:
        cli_memory.twin_id_for(tmp_path / "absent.json", None)

    assert caught.value.code == ErrorCode.MEMORY_PERSONA_UNREADABLE


async def test_initialize_indexes_builds_indexes_through_the_opened_memory() -> None:
    events: list[str] = []
    memory = make_graphiti()
    memory.events = events

    @asynccontextmanager
    async def open_memory() -> AsyncIterator[Any]:
        yield memory

    await initialize_indexes(open_memory)

    assert events == ["build"]


async def test_run_reembed_counts_the_operator_group(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def reembed(_memory: Any, *, group_id: str) -> ReembedResult:
        seen.append(group_id)
        return ReembedResult(entities=2, facts=5)

    monkeypatch.setattr(maintenance, "reembed_group", reembed)
    access = OperatorAccess(
        OperatorIdentity(write_persona(tmp_path), None, None),
        settings,
        lambda: tracked_memory([]),
    )

    result = await run_reembed(access)

    assert result == ReembedResult(entities=2, facts=5)
    assert seen == [PersonaProfile.model_validate(PERSONA_DATA).memory_group]
