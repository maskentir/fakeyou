import asyncio
import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

import fakeyou.cli.debrief as cli_debrief
import fakeyou.cli.diagnostics as cli_diagnostics
import fakeyou.cli.main as cli_main
from fakeyou.brief import (
    MeetingBrief,
    MeetingContext,
    brief_digest,
    context_path,
    write_meeting_context,
)
from fakeyou.cli.errors import CliError
from fakeyou.cli.main import build_parser
from fakeyou.debrief_schema import DebriefDraft
from fakeyou.diarize_client import DiarizedTurn
from fakeyou.diarize_correction import correction_uuid, write_correction_bundle
from fakeyou.ingestion_status import IngestionStatus, record_lock, status_path, write_status
from fakeyou.journal import Journal, journal_path
from fakeyou.persona import PersonaProfile, persona_digest
from fakeyou.record import TranscriptEntry, meeting_record_path
from fakeyou.settings import GoogleReply
from tests.fakes import ToolCallingFakeModel


async def test_debrief_cancellation_exits_nonzero_without_traceback(monkeypatch, capsys):
    async def cancel(**kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        "sys.argv", ["fakeyou", "debrief", "--meeting", "m1", "--persona", "p.json"]
    )
    monkeypatch.setattr(cli_main, "debrief_meeting", cancel)
    with pytest.raises(SystemExit) as error:
        await asyncio.to_thread(cli_main.main)
    assert error.value.code == 1
    assert "interrupted" in capsys.readouterr().err


@pytest.mark.parametrize("outcome", ["available", "stale", "missing", "invalid"])
def test_session_report_has_independent_debrief_status(settings, monkeypatch, capsys, outcome):
    journal = Journal(
        "debrief-test", path=journal_path(settings.paths.journal_dir, "debrief-test"), stdout=False
    )
    journal.record(
        "session_finished", source="cli", terminal_reason="stopped_by_owner", exit_code=0
    )
    journal.close()
    monkeypatch.setattr(cli_diagnostics, "debrief_report_status", lambda path: outcome)
    cli_diagnostics.show_session_report("debrief-test", None, settings)
    report = json.loads(capsys.readouterr().out)
    assert report["debrief"] == {"status": outcome}
    assert report["result"]["terminal_reason"] == "stopped_by_owner"


@pytest.fixture
def debrief_run(settings, tmp_path):
    profile = PersonaProfile(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        display_name="Demo Twin",
        role="Project coordinator",
    )
    persona_path = tmp_path / "persona.json"
    persona_path.write_text(profile.model_dump_json())
    record_path = meeting_record_path(settings.paths.record_dir, "debrief-test")
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        TranscriptEntry(
            at=datetime(2026, 10, 3, tzinfo=UTC),
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
            meeting_id="debrief-test", group_id=profile.memory_group, status="not_requested"
        ),
    )
    return (
        partial(
            cli_debrief.debrief_meeting,
            meeting_id="debrief-test",
            persona_path=persona_path,
            owner_id=None,
            twin_id=None,
            force=False,
            settings=settings,
        ),
        record_path,
        profile,
    )


def debrief_model():
    return ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "DebriefDraft",
                        "id": "draft-1",
                        "args": {
                            "items": [
                                {
                                    "kind": "owner_decision",
                                    "status": "needs_owner",
                                    "text": "The owner needs to decide about the new deadline.",
                                    "evidence": [
                                        {
                                            "entry_id": "transcript:t1",
                                            "quote": "move the deadline to Monday",
                                        }
                                    ],
                                }
                            ],
                            "findings": [],
                        },
                    }
                ],
            )
        ]
    )


async def test_debrief_creates_reviewable_report_and_cache_without_new_call(
    debrief_run, monkeypatch, capsys
):
    run, path, profile = debrief_run
    model = debrief_model()
    monkeypatch.setattr(cli_debrief, "create_debrief_chat_model", lambda settings: model)
    await run()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed"
    report = json.loads((path.parent / "debrief.json").read_text())
    assert report["review_status"] == "unreviewed"
    assert report["owner_id"] == profile.owner_id
    assert report["items"][0]["status"] == "needs_owner"
    assert report["evidence_sources"][0]["participant_id"] == "unknown"
    assert (path.parent / "debrief.md").exists()
    monkeypatch.setattr(
        cli_debrief,
        "create_debrief_chat_model",
        lambda settings: pytest.fail("cache must not call a provider"),
    )
    await run()
    assert json.loads(capsys.readouterr().out)["cached"] is True


async def test_debrief_report_names_the_configured_debrief_role(debrief_run, settings, monkeypatch):
    run, path, _ = debrief_run
    settings.debrief = GoogleReply(model="gemini-3.8-flash")
    monkeypatch.setattr(cli_debrief, "create_debrief_chat_model", lambda settings: debrief_model())
    await run()
    report = json.loads((path.parent / "debrief.json").read_text())
    assert (report["provider"], report["model"]) == ("google", "gemini-3.8-flash")


async def test_debrief_refuses_cross_scope_before_model(debrief_run, monkeypatch):
    run, path, profile = debrief_run
    write_status(
        status_path(path),
        IngestionStatus(meeting_id="debrief-test", group_id="other-owner", status="pending"),
    )
    monkeypatch.setattr(
        cli_debrief, "create_debrief_chat_model", lambda settings: pytest.fail("scope")
    )
    with pytest.raises(CliError):
        await run()
    assert not (path.parent / "debrief.json").exists()


async def test_debrief_refuses_unscoped_record_before_model(debrief_run, monkeypatch):
    run, path, profile = debrief_run
    status_path(path).unlink()
    monkeypatch.setattr(
        cli_debrief, "create_debrief_chat_model", lambda settings: pytest.fail("scope")
    )
    with pytest.raises(CliError):
        await run()


async def test_debrief_pending_recovery_refuses_provider(debrief_run, monkeypatch):
    run, path, profile = debrief_run
    path.with_name("debrief.json.recover.rollback").write_text("previous successful report")
    monkeypatch.setattr(
        cli_debrief, "create_debrief_chat_model", lambda settings: pytest.fail("recovery")
    )
    with pytest.raises(CliError, match="recovery"):
        await run()


async def test_debrief_refuses_live_writer_and_releases_after_failure(debrief_run, monkeypatch):
    run, path, profile = debrief_run
    monkeypatch.setattr(cli_debrief, "create_debrief_chat_model", lambda settings: debrief_model())
    with record_lock(path, purpose="meeting"):
        with pytest.raises(CliError):
            await run()
    await run()
    assert (path.parent / "debrief.json").exists()


async def test_force_failure_preserves_existing_canonical_report(debrief_run, monkeypatch):
    run, path, profile = debrief_run
    monkeypatch.setattr(cli_debrief, "create_debrief_chat_model", lambda settings: debrief_model())
    await run()
    previous = (path.parent / "debrief.json").read_bytes()

    async def fail(*args, **kwargs):
        raise ValueError("provider failed")

    monkeypatch.setattr(cli_debrief, "generate_debrief", fail)
    with pytest.raises(CliError):
        await run(force=True)
    assert (path.parent / "debrief.json").read_bytes() == previous
    assert json.loads((path.parent / "debrief-attempt.json").read_text())["status"] == "failed"
    with record_lock(path, purpose="debrief"):
        pass


async def test_attempt_write_failure_preserves_cancellation(debrief_run, monkeypatch):
    run, path, profile = debrief_run
    monkeypatch.setattr(cli_debrief, "create_debrief_chat_model", lambda settings: debrief_model())

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError

    def fail_write(*args, **kwargs):
        raise OSError("disk write failed")

    monkeypatch.setattr(cli_debrief, "generate_debrief", cancel)
    monkeypatch.setattr(cli_debrief, "write_atomically", fail_write)
    with pytest.raises(asyncio.CancelledError):
        await run()
    assert not (path.parent / "debrief.json").exists()
    with record_lock(path, purpose="debrief"):
        pass


async def test_debrief_corrupt_context_refuses_provider(debrief_run, monkeypatch):
    run, path, profile = debrief_run
    context_path(path).write_text("{broken")
    monkeypatch.setattr(
        cli_debrief, "create_debrief_chat_model", lambda settings: pytest.fail("context")
    )
    with pytest.raises(CliError):
        await run()
    assert not (path.parent / "debrief.json").exists()


def test_debrief_total_budget_comes_from_the_debrief_role_and_fallback(settings):
    settings.debrief = GoogleReply(timeout_seconds=60.0, max_retries=2)
    settings.reply_fallback = None

    assert cli_debrief.debrief_total_budget(settings, 2) == 360.0

    settings.reply_fallback = GoogleReply(timeout_seconds=15.0, max_retries=1)

    assert cli_debrief.debrief_total_budget(settings, 2) == 420.0


async def test_debrief_total_deadline_ignores_the_conversation_budget(
    debrief_run, settings, monkeypatch
):
    run, path, profile = debrief_run
    settings.conversation.response_timeout_seconds = 0.01
    settings.debrief = GoogleReply(timeout_seconds=5.0, max_retries=0)
    monkeypatch.setattr(cli_debrief, "create_debrief_chat_model", lambda settings: debrief_model())

    async def quick(*args, **kwargs):
        await asyncio.sleep(0.05)
        return DebriefDraft(items=(), findings=())

    monkeypatch.setattr(cli_debrief, "generate_debrief", quick)

    await run()

    assert (path.parent / "debrief.json").exists()


async def test_debrief_enforces_total_response_deadline(debrief_run, settings, monkeypatch):
    run, path, profile = debrief_run
    settings.conversation.response_timeout_seconds = 60.0
    settings.debrief = GoogleReply(timeout_seconds=0.01, max_retries=0)
    cancelled = asyncio.Event()
    monkeypatch.setattr(cli_debrief, "create_debrief_chat_model", lambda settings: debrief_model())

    async def slow(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(cli_debrief, "generate_debrief", slow)
    with pytest.raises(CliError, match="TimeoutError"):
        await run()
    assert cancelled.is_set()
    assert not (path.parent / "debrief.json").exists()
    with record_lock(path, purpose="debrief"):
        pass


async def test_model_cannot_publish_after_record_changes(debrief_run, monkeypatch):
    run, path, profile = debrief_run
    monkeypatch.setattr(cli_debrief, "create_debrief_chat_model", lambda settings: debrief_model())

    async def mutate(*args, **kwargs):
        path.write_text(path.read_text() + "\n")
        return DebriefDraft(items=(), findings=())

    monkeypatch.setattr(cli_debrief, "generate_debrief", mutate)
    with pytest.raises(CliError):
        await run()
    assert not (path.parent / "debrief.json").exists()


async def test_debrief_uses_historical_brief_not_changed_persona(debrief_run, monkeypatch):
    run, path, profile = debrief_run
    brief = MeetingBrief(
        schema_version="meeting_brief/v1",
        owner_id=profile.owner_id,
        twin_id=profile.twin_id,
        meeting_id="debrief-test",
    )
    write_meeting_context(
        context_path(path),
        MeetingContext(
            schema_version="meeting_context/v1",
            brief=brief,
            brief_sha256=brief_digest(brief),
            persona_sha256=persona_digest(profile.model_copy(update={"role": "Previous role"})),
        ),
    )
    monkeypatch.setattr(cli_debrief, "create_debrief_chat_model", lambda settings: debrief_model())
    await run()
    assert json.loads((path.parent / "debrief.json").read_text())["context_sha256"] is not None


async def test_debrief_cancellation_keeps_previous_result_and_lock(debrief_run, monkeypatch):
    run, path, profile = debrief_run
    monkeypatch.setattr(cli_debrief, "create_debrief_chat_model", lambda settings: debrief_model())
    started = asyncio.Event()

    async def wait(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(cli_debrief, "generate_debrief", wait)
    task = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (path.parent / "debrief.json").exists()
    assert json.loads((path.parent / "debrief-attempt.json").read_text())["status"] == "interrupted"
    with record_lock(path, purpose="debrief"):
        pass


def test_debrief_parser_uses_operator_scope_and_force():
    parsed = build_parser().parse_args(
        ["debrief", "--meeting", "m1", "--persona", "persona.json", "--force"]
    )
    assert parsed.meeting_id == "m1"
    assert parsed.force is True
    assert parsed.persona_path == Path("persona.json")


async def test_debrief_renders_display_claim_as_unverified_speaker_label(
    settings, tmp_path, monkeypatch
):
    profile = PersonaProfile(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        display_name="Demo Twin",
        role="Project coordinator",
    )
    persona_path = tmp_path / "persona.json"
    persona_path.write_text(profile.model_dump_json())
    record_path = meeting_record_path(settings.paths.record_dir, "debrief-claim")
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        TranscriptEntry(
            at=datetime(2026, 10, 3, tzinfo=UTC),
            id="t1",
            spoken_at="2026-10-03T00:00:00+00:00",
            source="meeting_audio",
            text="The owner must decide whether to move the deadline to Monday.",
            participant_id="speaker-b",
            speaker_evidence_id=None,
            display_claim="Maya",
        ).model_dump_json()
        + "\n"
    )
    write_status(
        status_path(record_path),
        IngestionStatus(
            meeting_id="debrief-claim", group_id=profile.memory_group, status="not_requested"
        ),
    )
    monkeypatch.setattr(cli_debrief, "create_debrief_chat_model", lambda settings: debrief_model())
    await cli_debrief.debrief_meeting(
        meeting_id="debrief-claim",
        persona_path=persona_path,
        owner_id=None,
        twin_id=None,
        force=False,
        settings=settings,
    )
    report = json.loads((record_path.parent / "debrief.json").read_text())
    assert any(
        "shown as Maya, unverified" in limitation for limitation in report["source_limitations"]
    )
    assert "shown as Maya, unverified" in (record_path.parent / "debrief.md").read_text()


async def test_corrected_uuid_attribution_keeps_unknown_participant_limitation(
    debrief_run, monkeypatch
):
    run, path, _profile = debrief_run
    audio_dir = path.parent / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: datetime(2026, 10, 3, tzinfo=UTC),
    )
    monkeypatch.setattr(cli_debrief, "create_debrief_chat_model", lambda settings: debrief_model())
    await run()
    report = json.loads((path.parent / "debrief.json").read_text())
    assert report["evidence_sources"][0]["participant_id"] == correction_uuid("A", 0)
    assert any(
        "Unknown participants have not been identified" in limitation
        for limitation in report["source_limitations"]
    )


async def test_stale_sidecar_fallback_is_visible_in_journal(debrief_run, settings, monkeypatch):
    run, path, _profile = debrief_run
    audio_dir = path.parent / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: datetime(2026, 10, 3, tzinfo=UTC),
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            TranscriptEntry(
                at=datetime(2026, 10, 3, tzinfo=UTC),
                id="t2",
                spoken_at="2026-10-03T00:01:00+00:00",
                source="meeting_audio",
                text="A second voice notes the Monday deadline.",
                participant_id="unknown",
                speaker_evidence_id=None,
            ).model_dump_json()
            + "\n"
        )
    monkeypatch.setattr(cli_debrief, "create_debrief_chat_model", lambda settings: debrief_model())
    await run()
    report = json.loads((path.parent / "debrief.json").read_text())
    assert report["evidence_sources"][0]["participant_id"] == "unknown"
    rows = [
        json.loads(line)
        for line in journal_path(settings.paths.journal_dir, "debrief-test")
        .read_text()
        .splitlines()
        if line.strip()
    ]
    fallbacks = [
        row
        for row in rows
        if row["event"] == "fault_recorded" and row["source"] == "meeting_record"
    ]
    assert len(fallbacks) >= 1
    assert fallbacks[0]["detail"] == "StaleDiarizationError"
