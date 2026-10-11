import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

import fakeyou.cli.join as cli_join
import fakeyou.orchestration.assembly as assembly
import fakeyou.orchestration.providers as providers
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.foundation.invariants import AGENT_SINK_NAME, MEETING_MONITOR_SOURCE_NAME

LINK = "https://meet.google.com/abc-defg-hij"

PERSONA_DATA = {
    "owner_id": "settings-owner",
    "twin_id": "settings-twin",
    "display_name": "Rani Twin",
    "role": "Product lead for the synthetic Orchid project",
    "verified_facts": ["The Orchid beta launch is planned for 14 October 2026"],
    "allowed_tools": ["recall_meeting_context"],
}

BRIEF_DATA = {
    "schema_version": "meeting_brief/v1",
    "owner_id": "settings-owner",
    "twin_id": "settings-twin",
    "meeting_id": "meeting-9",
    "agenda": ["Synthetic Orchid kickoff agenda item"],
    "objectives": [{"id": "blockers", "text": "Find the synthetic kickoff blocker."}],
    "approved_facts": [{"id": "demo-date", "text": "The synthetic Orchid demo is booked."}],
    "questions": [{"id": "vendor-needs", "text": "Which synthetic vendor input is missing?"}],
    "permissions": ["explain_facts", "ask_brief_questions"],
    "knowledge_scope": "brief_only",
}


def write_persona(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "persona.json"
    path.write_text(json.dumps({**PERSONA_DATA, **overrides}), encoding="utf-8")
    return path


def write_brief(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "brief.json"
    path.write_text(json.dumps({**BRIEF_DATA, **overrides}), encoding="utf-8")
    return path


def seed_unfinished_session(settings: Settings) -> None:
    from fakeyou.observability.journal import journal_path
    from fakeyou.persistence.record import meeting_record_path

    record = meeting_record_path(settings.paths.record_dir, "meeting-9")
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        '{"kind": "transcript", "at": "2026-10-05T02:00:00+00:00",'
        ' "id": "t1", "spoken_at": "2026-10-05T02:00:00+00:00",'
        ' "source": "meeting_audio", "text": "Hello.",'
        ' "participant_id": "unknown", "speaker_evidence_id": null}\n'
        '{"kind": "twin_speech", "at": "2026-10-05T02:00:05+00:00",'
        ' "text": "Hi there."}\n',
        encoding="utf-8",
    )
    journal = journal_path(settings.paths.journal_dir, "meeting-9")
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        '{"event": "join_requested", "meeting_id": "meeting-9",'
        ' "at": "2026-10-05T02:00:00+00:00", "source": "runtime"}\n',
        encoding="utf-8",
    )


def seed_finished_session(settings: Settings) -> None:
    from fakeyou.observability.journal import journal_path

    seed_unfinished_session(settings)
    journal = journal_path(settings.paths.journal_dir, "meeting-9")
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"event": "session_finished", "meeting_id": "meeting-9",'
            ' "at": "2026-10-05T02:10:00+00:00", "source": "cli",'
            ' "phase": "ended", "terminal_reason": "ended_by_host", "exit_code": 0}\n'
        )


@pytest.mark.asyncio
async def test_join_refuses_an_unfinished_meeting_without_resume(
    settings: Settings, join: Any, session_run: Any
) -> None:
    seed_unfinished_session(settings)

    with pytest.raises(FakeYouError, match="unfinished session") as error:
        await join(LINK, meeting_id="meeting-9")

    assert error.value.code is ErrorCode.MEETING_UNFINISHED_SESSION

    assert "config" not in session_run


@pytest.mark.asyncio
async def test_join_resume_writes_markers_and_rejoins(
    settings: Settings, join: Any, session_run: Any
) -> None:
    from fakeyou.observability.journal import journal_path
    from fakeyou.persistence.record import load_meeting_record, meeting_record_path

    seed_unfinished_session(settings)

    await join(LINK, meeting_id="meeting-9", resume=True)

    assert session_run["config"].meeting_id == "meeting-9"
    assert session_run["config"].resume is True
    journal_rows = journal_path(settings.paths.journal_dir, "meeting-9").read_text(encoding="utf-8")
    assert '"event":"session_resumed"' in journal_rows.replace(" ", "")
    entries = load_meeting_record(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    assert entries[-1].kind == "resume"
    assert entries[-1].attempt == 2


@pytest.mark.asyncio
async def test_join_resume_detects_a_live_checkpoint_without_journal(
    settings: Settings, join: Any, session_run: Any
) -> None:
    from fakeyou.db.checkpoint import open_checkpointer
    from fakeyou.db.engine import configured_url, require_url
    from fakeyou.observability.journal import journal_path
    from fakeyou.orchestration.lifecycle import MeetingLifecycle
    from fakeyou.persistence.record import load_meeting_record, meeting_record_path

    async with open_checkpointer(require_url(configured_url(settings))) as saver:
        first = MeetingLifecycle("settings-owner", "settings-twin", "meeting-9", saver)
        await first.initialize()
        await first.send("join_requested", "owner_action", "test")

    await join(LINK, meeting_id="meeting-9", resume=True)

    assert session_run["config"].meeting_id == "meeting-9"
    journal_rows = journal_path(settings.paths.journal_dir, "meeting-9").read_text(encoding="utf-8")
    assert '"event":"session_resumed"' in journal_rows.replace(" ", "")
    entries = load_meeting_record(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    assert entries[-1].kind == "resume"


@pytest.mark.asyncio
async def test_join_resume_refuses_a_finished_meeting(
    settings: Settings, join: Any, session_run: Any
) -> None:
    seed_finished_session(settings)

    with pytest.raises(FakeYouError, match="already ended") as error:
        await join(LINK, meeting_id="meeting-9", resume=True)

    assert error.value.code is ErrorCode.MEETING_ALREADY_ENDED

    assert "config" not in session_run


@pytest.mark.asyncio
async def test_join_resume_requires_the_same_brief_and_persona(
    settings: Settings, join: Any, session_run: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("PULSE_SOURCE", MEETING_MONITOR_SOURCE_NAME)
    monkeypatch.setenv("PULSE_SINK", AGENT_SINK_NAME)
    monkeypatch.setattr(cli_join, "find_spec", lambda name: SimpleNamespace())
    monkeypatch.setattr(providers, "init_chat_model", lambda *args, **kwargs: SimpleNamespace())

    @asynccontextmanager
    async def fake_saver(url: str) -> AsyncIterator[InMemorySaver]:
        yield InMemorySaver()

    monkeypatch.setattr(assembly, "open_checkpointer", fake_saver)
    monkeypatch.setattr(
        assembly, "create_meeting_agent", lambda *a, **k: SimpleNamespace(name="agent")
    )
    from fakeyou.memory.persona import PersonaProfile, persona_digest
    from fakeyou.persistence.brief import (
        MeetingBrief,
        MeetingContext,
        brief_digest,
        context_path,
        write_meeting_context,
    )
    from fakeyou.persistence.record import meeting_record_path

    seed_unfinished_session(settings)
    brief = MeetingBrief.model_validate(BRIEF_DATA)
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    write_meeting_context(
        context_path(meeting_record_path(settings.paths.record_dir, "meeting-9")),
        MeetingContext(
            schema_version="meeting_context/v1",
            brief=brief,
            brief_sha256=brief_digest(brief),
            persona_sha256=persona_digest(profile),
        ),
    )

    live = {"audio_enabled": True, "conversation_enabled": True}
    with pytest.raises(FakeYouError, match="same --brief and --persona") as error:
        await join(LINK, meeting_id="meeting-9", resume=True, **live)

    assert error.value.code is ErrorCode.MEETING_STARTED_WITH_BRIEF

    other_brief = write_brief(tmp_path, agenda=["Something else entirely"])
    with pytest.raises(FakeYouError, match="differs from --brief or --persona") as error:
        await join(
            LINK,
            meeting_id="meeting-9",
            resume=True,
            persona_path=write_persona(tmp_path),
            brief_path=other_brief,
            **live,
        )

    assert error.value.code is ErrorCode.MEETING_CONTEXT_DIFFERS

    await join(
        LINK,
        meeting_id="meeting-9",
        resume=True,
        persona_path=write_persona(tmp_path),
        brief_path=write_brief(tmp_path),
        **live,
    )
    assert session_run["config"].meeting_id == "meeting-9"
