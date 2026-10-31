import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import pytest

import fakeyou.cli.memory as cli_memory
from fakeyou.catalog.errors import CATALOG, ErrorCode
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.client import ApiFailure, ClientSettings
from fakeyou.cli.memory import NoteDraft
from fakeyou.config.settings import Settings
from fakeyou.memory import maintenance
from fakeyou.memory.maintenance import initialize_indexes, run_reembed
from fakeyou.memory.memory import ReembedResult
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from tests._cli_memory_support import tracked_memory
from tests._cli_support import PERSONA_DATA, write_persona
from tests._memory_fakes import make_graphiti

EPISODE = "a1b2c3d4-0000-4000-8000-000000000001"
OPERATION = UUID("a1b2c3d4-0000-4000-8000-000000000002")
SOURCE_AT = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
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


def _note_view() -> dict[str, Any]:
    return {
        "episode_uuid": EPISODE,
        "facts": ["Orchid moved to Friday"],
        "source_at": SOURCE_AT.isoformat(),
        "recorded_at": SOURCE_AT.isoformat(),
        "supersedes": None,
    }


async def test_memory_add_posts_the_note_with_a_generated_operation_id(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx2.Request] = []
    monkeypatch.setattr(cli_memory, "uuid4", lambda: OPERATION)
    transport = _route(
        {("POST", "/v1/twins/twin-1/notes"): httpx2.Response(201, json=_note_view())}, seen
    )

    await cli_memory.add_owner_note(
        NoteDraft(text="Orchid moved", twin_id="twin-1"), settings=SETTINGS, transport=transport
    )

    body = json.loads(seen[0].content)
    assert body == {"text": "Orchid moved", "source_at": None, "note_id": str(OPERATION)}
    assert seen[0].headers["authorization"] == "Bearer owner-secret"
    captured = capsys.readouterr()
    assert f"fakeyou: note-id {OPERATION}" in captured.err
    assert json.loads(captured.out) == _note_view()


async def test_memory_add_does_not_announce_a_caller_supplied_note_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[httpx2.Request] = []
    transport = _route(
        {("POST", "/v1/twins/twin-1/notes"): httpx2.Response(201, json=_note_view())}, seen
    )

    await cli_memory.add_owner_note(
        NoteDraft(text="x", twin_id="twin-1", source_at=SOURCE_AT, note_id=OPERATION),
        settings=SETTINGS,
        transport=transport,
    )

    assert "note-id" not in capsys.readouterr().err
    assert json.loads(seen[0].content)["source_at"] == SOURCE_AT.isoformat()


async def test_memory_add_maps_a_note_conflict_to_its_catalog_exit() -> None:
    conflict = httpx2.Response(
        409,
        json={"error": {"code": "note.id_in_use", "message": "note id already used"}},
    )
    transport = _route({("POST", "/v1/twins/twin-1/notes"): conflict}, [])

    with pytest.raises(ApiFailure, match="note id already used") as caught:
        await cli_memory.add_owner_note(
            NoteDraft(text="x", twin_id="twin-1", note_id=OPERATION),
            settings=SETTINGS,
            transport=transport,
        )

    assert caught.value.code == "note.id_in_use"
    assert caught.value.exit_code is CATALOG[ErrorCode.NOTE_ID_IN_USE].exit_code


async def test_memory_correct_targets_the_corrections_route_for_the_episode() -> None:
    seen: list[httpx2.Request] = []
    transport = _route(
        {
            ("POST", f"/v1/twins/twin-1/notes/{EPISODE}/corrections"): httpx2.Response(
                201, json=_note_view()
            )
        },
        seen,
    )

    await cli_memory.correct_owner_note(
        NoteDraft(text="fixed", twin_id="twin-1", source_at=SOURCE_AT, note_id=OPERATION),
        episode_uuid=EPISODE,
        settings=SETTINGS,
        transport=transport,
    )

    assert json.loads(seen[0].content) == {
        "text": "fixed",
        "source_at": SOURCE_AT.isoformat(),
        "note_id": str(OPERATION),
    }


async def test_memory_correct_requires_a_source_time_before_calling_the_api() -> None:
    seen: list[httpx2.Request] = []
    transport = _route({}, seen)

    with pytest.raises(ApiFailure):
        await cli_memory.correct_owner_note(
            NoteDraft(text="fixed", twin_id="twin-1", note_id=OPERATION),
            episode_uuid=EPISODE,
            settings=SETTINGS,
            transport=transport,
        )

    assert seen == []


async def test_memory_forget_episode_sends_delete_for_the_episode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[httpx2.Request] = []
    transport = _route(
        {("DELETE", f"/v1/twins/twin-1/notes/{EPISODE}"): httpx2.Response(204)}, seen
    )

    await cli_memory.forget_episode_memory(
        twin_id="twin-1", episode_uuid=EPISODE, settings=SETTINGS, transport=transport
    )

    assert [request.method for request in seen] == ["DELETE"]
    assert json.loads(capsys.readouterr().out) == {"removed_episodes": 1}


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
