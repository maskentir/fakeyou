import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import fakeyou.debrief.debrief_service as debrief_service
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import GoogleReply, Settings
from fakeyou.debrief.debrief import DebriefModels
from fakeyou.debrief.debrief_schema import DebriefDraft
from fakeyou.debrief.debrief_service import (
    DebriefRequest,
    DebriefWiring,
    debrief_total_budget,
    run_debrief,
)
from fakeyou.memory.persona import PersonaProfile
from fakeyou.orchestration.assembly import SignalHandlers
from fakeyou.persistence.ingestion_status import IngestionStatus, status_path, write_status
from fakeyou.persistence.record import TranscriptEntry, meeting_record_path
from tests._conversation_fakes import ToolCallingFakeModel
from tests._error_support import message_pattern


@contextmanager
def _quiet_signals(stop: asyncio.Event) -> Iterator[SignalHandlers]:
    del stop
    yield SignalHandlers(installed=(), unavailable=("SIGINT", "SIGTERM"))


def _profile(settings: Settings) -> PersonaProfile:
    return PersonaProfile(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        display_name="Demo Twin",
        role="Project coordinator",
    )


def _seed_meeting(settings: Settings, meeting_id: str, profile: PersonaProfile) -> Path:
    record_path = meeting_record_path(settings.paths.record_dir, SessionId(meeting_id))
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        TranscriptEntry(
            at=datetime(2026, 10, 3, tzinfo=UTC),
            seq=1,
            id="t1",
            spoken_at="2026-10-03T00:00:00+00:00",
            source="meeting_audio",
            text="The owner must decide whether to move the deadline to Monday.",
            participant_id="unknown",
            speaker_evidence_id=None,
        ).model_dump_json()
        + "\n"
    )
    write_status(
        status_path(record_path),
        IngestionStatus(
            meeting_id=meeting_id, group_id=profile.memory_group, status="not_requested"
        ),
    )
    return record_path


async def test_run_debrief_publishes_then_serves_cache_without_building_models(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(settings)
    record_path = _seed_meeting(settings, "debrief-service", profile)

    async def empty_draft(*_args: Any, **_kwargs: Any) -> DebriefDraft:
        return DebriefDraft(items=(), findings=())

    monkeypatch.setattr(debrief_service, "generate_debrief", empty_draft)
    request = DebriefRequest(
        meeting_id=SessionId("debrief-service"),
        operator=profile,
        settings=settings,
        force=False,
    )
    first = await run_debrief(
        request,
        DebriefWiring(
            models=lambda: DebriefModels(ToolCallingFakeModel(responses=[])),
            stop_signals=_quiet_signals,
        ),
    )
    assert first.cached is False
    assert first.json_path == record_path.parent / "debrief.json"
    assert first.json_path.is_file()

    second = await run_debrief(
        request,
        DebriefWiring(
            models=lambda: pytest.fail("cache must not build models"),
            stop_signals=_quiet_signals,
        ),
    )
    assert second.cached is True
    assert second.markdown_path == record_path.parent / "debrief.md"


async def test_run_debrief_refuses_missing_record_before_building_models(
    settings: Settings,
) -> None:
    request = DebriefRequest(
        meeting_id=SessionId("debrief-absent"),
        operator=_profile(settings),
        settings=settings,
        force=False,
    )
    wiring = DebriefWiring(
        models=lambda: pytest.fail("record must be checked first"),
        stop_signals=_quiet_signals,
    )
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.DEBRIEF_RECORD_MISSING)
    ) as caught:
        await run_debrief(request, wiring)
    assert caught.value.code is ErrorCode.DEBRIEF_RECORD_MISSING


def test_debrief_total_budget_comes_from_the_debrief_role_and_fallback(settings: Settings) -> None:
    settings.debrief = GoogleReply(timeout_seconds=60.0, max_retries=2)
    settings.reply_fallback = None

    assert debrief_total_budget(settings, 2) == 360.0

    settings.reply_fallback = GoogleReply(timeout_seconds=15.0, max_retries=1)

    assert debrief_total_budget(settings, 2) == 420.0
