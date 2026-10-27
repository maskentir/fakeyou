import asyncio
import json
import sys
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, NoReturn

import pytest
from langchain_core.messages import AIMessage

import fakeyou.cli.main as cli_main
import fakeyou.debrief.debrief_service as debrief_service
import fakeyou.orchestration.providers as providers
import tests._debrief_runner as debrief_support
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import GoogleReply, Settings
from fakeyou.debrief.debrief import DebriefStopRequested
from fakeyou.debrief.debrief_schema import DebriefDraft
from fakeyou.diarization.diarize_client import DiarizedTurn
from fakeyou.diarization.diarize_correction import correction_uuid, write_correction_bundle
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile, persona_digest
from fakeyou.observability.journal import journal_path
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.orchestration.assembly import SignalHandlers
from fakeyou.persistence.brief import (
    MeetingBrief,
    MeetingContext,
    brief_digest,
    context_path,
    write_meeting_context,
)
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    record_lock,
    status_path,
    write_status,
)
from fakeyou.persistence.record import TranscriptEntry, meeting_record_path
from tests._conversation_fakes import ToolCallingFakeModel
from tests._debrief_runner import debrief_in_process
from tests._error_support import message_pattern


async def test_debrief_cancellation_exits_nonzero_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def cancel(**_kwargs: Any) -> NoReturn:
        raise asyncio.CancelledError

    monkeypatch.setattr("sys.argv", ["fakeyou", "debrief", "--meeting-id", "m1"])
    monkeypatch.setattr(cli_main, "debrief_meeting", cancel)
    with pytest.raises(SystemExit) as error:
        await asyncio.to_thread(cli_main.main)
    assert error.value.code == 1
    assert "interrupted" in capsys.readouterr().err


@pytest.fixture
def debrief_run(
    settings: Settings, tmp_path: Path
) -> tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile]:
    profile = PersonaProfile(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        display_name="Demo Twin",
        role="Project coordinator",
    )
    persona_path = tmp_path / "persona.json"
    persona_path.write_text(profile.model_dump_json())
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("debrief-test"))
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
            meeting_id="debrief-test", group_id=profile.memory_group, status="not_requested"
        ),
    )
    return (
        partial(
            debrief_in_process,
            meeting_id=SessionId("debrief-test"),
            identity=OperatorIdentity(persona_path, None, None),
            force=False,
            settings=settings,
        ),
        record_path,
        profile,
    )


def debrief_model() -> ToolCallingFakeModel:
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
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run, path, profile = debrief_run
    model = debrief_model()
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: model)
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
        providers,
        "create_debrief_chat_model",
        lambda _settings: pytest.fail("cache must not call a provider"),
    )
    await run()
    assert json.loads(capsys.readouterr().out)["cached"] is True


async def test_debrief_report_names_the_configured_debrief_role(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, _ = debrief_run
    settings.debrief = GoogleReply(model="gemini-3.8-flash")
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())
    await run()
    report = json.loads((path.parent / "debrief.json").read_text())
    assert (report["provider"], report["model"]) == ("google", "gemini-3.8-flash")


async def test_debrief_refuses_cross_scope_before_model(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    write_status(
        status_path(path),
        IngestionStatus(meeting_id="debrief-test", group_id="other-owner", status="pending"),
    )
    monkeypatch.setattr(
        providers, "create_debrief_chat_model", lambda _settings: pytest.fail("scope")
    )
    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.DEBRIEF_SCOPE_MISMATCH),
    ) as caught:
        await run()
    assert caught.value.code is ErrorCode.DEBRIEF_SCOPE_MISMATCH
    assert not (path.parent / "debrief.json").exists()


async def test_debrief_refuses_unscoped_record_before_model(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    status_path(path).unlink()
    monkeypatch.setattr(
        providers, "create_debrief_chat_model", lambda _settings: pytest.fail("scope")
    )
    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.DEBRIEF_OWNERSHIP_UNVERIFIABLE),
    ) as caught:
        await run()
    assert caught.value.code is ErrorCode.DEBRIEF_OWNERSHIP_UNVERIFIABLE


async def test_debrief_pending_recovery_refuses_provider(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    path.with_name("debrief.json.recover.rollback").write_text("previous successful report")
    monkeypatch.setattr(
        providers, "create_debrief_chat_model", lambda _settings: pytest.fail("recovery")
    )
    with pytest.raises(FakeYouError, match="recovery") as caught:
        await run()
    assert caught.value.code is ErrorCode.DEBRIEF_PUBLICATION_RECOVERY


async def test_debrief_refuses_live_writer_and_releases_after_failure(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())
    with record_lock(path, purpose="meeting"):
        with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.RECORD_LOCKED)) as caught:
            await run()
        assert caught.value.code is ErrorCode.RECORD_LOCKED
    await run()
    assert (path.parent / "debrief.json").exists()


async def test_force_failure_preserves_existing_canonical_report(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())
    await run()
    previous = (path.parent / "debrief.json").read_bytes()

    async def fail(*args: Any, **_kwargs: Any) -> NoReturn:
        del args
        raise ValueError("provider failed")

    monkeypatch.setattr(debrief_service, "generate_debrief", fail)
    with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.DEBRIEF_FAILED)) as caught:
        await run(force=True)
    assert caught.value.code is ErrorCode.DEBRIEF_FAILED
    assert (path.parent / "debrief.json").read_bytes() == previous
    assert json.loads((path.parent / "debrief-attempt.json").read_text())["status"] == "failed"
    with record_lock(path, purpose="debrief"):
        pass


async def test_attempt_write_failure_preserves_cancellation(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())

    async def cancel(*args: Any, **_kwargs: Any) -> NoReturn:
        del args
        raise asyncio.CancelledError

    def fail_write(*args: Any, **_kwargs: Any) -> NoReturn:
        del args
        raise OSError("disk write failed")

    monkeypatch.setattr(debrief_service, "generate_debrief", cancel)
    monkeypatch.setattr(debrief_service, "write_atomically", fail_write)
    with pytest.raises(asyncio.CancelledError):
        await run()
    assert not (path.parent / "debrief.json").exists()
    with record_lock(path, purpose="debrief"):
        pass


async def test_provider_faults_keep_their_identity_through_debrief(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run

    def unavailable(_settings: Any) -> NoReturn:
        raise ProviderFault("llm", "unavailable")

    monkeypatch.setattr(providers, "create_debrief_chat_model", unavailable)
    with pytest.raises(ProviderFault) as caught:
        await run()
    assert caught.value.kind == "unavailable"


async def test_debrief_corrupt_context_refuses_provider(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    context_path(path).write_text("{broken")
    monkeypatch.setattr(
        providers, "create_debrief_chat_model", lambda _settings: pytest.fail("context")
    )
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.DEBRIEF_CONTEXT_INVALID)
    ) as caught:
        await run()
    assert caught.value.code is ErrorCode.DEBRIEF_CONTEXT_INVALID
    assert not (path.parent / "debrief.json").exists()


async def test_debrief_total_deadline_ignores_the_conversation_budget(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    settings.conversation.response_timeout_seconds = 0.01
    settings.debrief = GoogleReply(timeout_seconds=5.0, max_retries=0)
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())

    async def quick(*args: Any, **_kwargs: Any) -> DebriefDraft:
        del args
        await asyncio.sleep(0.05)
        return DebriefDraft(items=(), findings=())

    monkeypatch.setattr(debrief_service, "generate_debrief", quick)

    await run()

    assert (path.parent / "debrief.json").exists()


async def test_debrief_enforces_total_response_deadline(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    settings.conversation.response_timeout_seconds = 60.0
    settings.debrief = GoogleReply(timeout_seconds=0.01, max_retries=0)
    cancelled = asyncio.Event()
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())

    async def slow(*args: Any, **_kwargs: Any) -> None:
        del args
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(debrief_service, "generate_debrief", slow)
    with pytest.raises(FakeYouError, match="TimeoutError") as caught:
        await run()
    assert caught.value.code is ErrorCode.DEBRIEF_FAILED
    assert cancelled.is_set()
    assert not (path.parent / "debrief.json").exists()
    with record_lock(path, purpose="debrief"):
        pass


async def test_model_cannot_publish_after_record_changes(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())

    async def mutate(*args: Any, **_kwargs: Any) -> DebriefDraft:
        del args
        path.write_text(path.read_text() + "\n")
        return DebriefDraft(items=(), findings=())

    monkeypatch.setattr(debrief_service, "generate_debrief", mutate)
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.DEBRIEF_SOURCES_CHANGED)
    ) as caught:
        await run()
    assert caught.value.code is ErrorCode.DEBRIEF_SOURCES_CHANGED
    assert not (path.parent / "debrief.json").exists()


async def test_debrief_uses_historical_brief_not_changed_persona(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    brief = MeetingBrief(
        schema_version="meeting_brief/v1",
        owner_id=profile.owner_id,
        twin_id=profile.twin_id,
        meeting_id=SessionId("debrief-test"),
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
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())
    await run()
    assert json.loads((path.parent / "debrief.json").read_text())["context_sha256"] is not None


async def test_owner_stop_raises_interrupted_with_interrupted_status(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())

    async def stopped(*args: Any, **_kwargs: Any) -> NoReturn:
        del args
        raise DebriefStopRequested

    monkeypatch.setattr(debrief_service, "generate_debrief", stopped)
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.OPERATION_INTERRUPTED)
    ) as caught:
        await run()
    assert caught.value.code is ErrorCode.OPERATION_INTERRUPTED
    assert not (path.parent / "debrief.json").exists()
    assert json.loads((path.parent / "debrief-attempt.json").read_text())["status"] == "interrupted"


async def test_stop_set_after_generation_still_records_interrupted(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())
    entered = asyncio.Event()
    proceed = asyncio.Event()
    stops: list[asyncio.Event] = []

    @contextmanager
    def capture(stop: asyncio.Event) -> Iterator[SignalHandlers]:
        stops.append(stop)
        yield SignalHandlers(installed=(), unavailable=("SIGINT", "SIGTERM"))

    async def gen(*args: Any, **_kwargs: Any) -> DebriefDraft:
        del args
        entered.set()
        await proceed.wait()
        return DebriefDraft(items=(), findings=())

    monkeypatch.setattr(debrief_support, "stop_signal_handlers", capture)
    monkeypatch.setattr(debrief_service, "generate_debrief", gen)
    task = asyncio.create_task(run())
    await asyncio.wait_for(entered.wait(), 2)
    stops[0].set()
    proceed.set()
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.OPERATION_INTERRUPTED)
    ) as caught:
        await task
    assert caught.value.code is ErrorCode.OPERATION_INTERRUPTED
    assert json.loads((path.parent / "debrief-attempt.json").read_text())["status"] == "interrupted"


async def test_debrief_cancellation_keeps_previous_result_and_lock(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())
    started = asyncio.Event()

    async def wait(*args: Any, **_kwargs: Any) -> None:
        del args
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(debrief_service, "generate_debrief", wait)
    task = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not (path.parent / "debrief.json").exists()
    assert json.loads((path.parent / "debrief-attempt.json").read_text())["status"] == "interrupted"
    with record_lock(path, purpose="debrief"):
        pass


def test_debrief_dispatch_sends_meeting_and_force_to_the_api_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_debrief(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, "debrief_meeting", fake_debrief)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "debrief", "--meeting-id", "m1", "--force"])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert call["meeting_id"] == "m1"
    assert call["force"] is True


async def test_debrief_renders_display_claim_as_unverified_speaker_label(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = PersonaProfile(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        display_name="Demo Twin",
        role="Project coordinator",
    )
    persona_path = tmp_path / "persona.json"
    persona_path.write_text(profile.model_dump_json())
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("debrief-claim"))
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        TranscriptEntry(
            at=datetime(2026, 10, 3, tzinfo=UTC),
            seq=1,
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
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())
    await debrief_in_process(
        meeting_id=SessionId("debrief-claim"),
        identity=OperatorIdentity(persona_path, None, None),
        force=False,
        settings=settings,
    )
    report = json.loads((record_path.parent / "debrief.json").read_text())
    assert any(
        "shown as Maya, unverified" in limitation for limitation in report["source_limitations"]
    )
    assert "shown as Maya, unverified" in (record_path.parent / "debrief.md").read_text()


async def test_corrected_uuid_attribution_keeps_unknown_participant_limitation(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, _profile = debrief_run
    audio_dir = path.parent / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: datetime(2026, 10, 3, tzinfo=UTC),
    )
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())
    await run()
    report = json.loads((path.parent / "debrief.json").read_text())
    assert report["evidence_sources"][0]["participant_id"] == correction_uuid("A", 0)
    assert any(
        "Unknown participants have not been identified" in limitation
        for limitation in report["source_limitations"]
    )


async def test_stale_sidecar_fallback_is_visible_in_journal(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, _profile = debrief_run
    audio_dir = path.parent / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: datetime(2026, 10, 3, tzinfo=UTC),
    )
    rows = path.read_text(encoding="utf-8").splitlines(keepends=True)
    last = json.loads(rows[-1])
    last["capture_ms"] = 1.0
    rows[-1] = json.dumps(last) + "\n"
    path.write_text("".join(rows), encoding="utf-8")
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())
    await run()
    report = json.loads((path.parent / "debrief.json").read_text())
    assert report["evidence_sources"][0]["participant_id"] == "unknown"
    journal_rows = [
        json.loads(line)
        for line in journal_path(settings.paths.journal_dir, SessionId("debrief-test"))
        .read_text()
        .splitlines()
        if line.strip()
    ]
    fallbacks = [
        row
        for row in journal_rows
        if row["event"] == "fault_recorded" and row["source"] == "meeting_record"
    ]
    assert len(fallbacks) >= 1
    assert fallbacks[0]["detail"] == "diarize.stale"
    [bound] = [row for row in journal_rows if row["event"] == "signal_handlers_bound"]
    assert bound["signals"] == ["SIGINT", "SIGTERM"]
    assert bound["unavailable"] == []


async def test_debrief_refuses_a_corrupt_status_and_journals_it(
    debrief_run: tuple[partial[Coroutine[Any, Any, None]], Path, PersonaProfile],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, path, profile = debrief_run
    brief = MeetingBrief(
        schema_version="meeting_brief/v1",
        owner_id=profile.owner_id,
        twin_id=profile.twin_id,
        meeting_id=SessionId("debrief-test"),
    )
    write_meeting_context(
        context_path(path),
        MeetingContext(
            schema_version="meeting_context/v1",
            brief=brief,
            brief_sha256=brief_digest(brief),
            persona_sha256=persona_digest(profile),
        ),
    )
    status_path(path).write_text("{corrupt", encoding="utf-8")
    monkeypatch.setattr(providers, "create_debrief_chat_model", lambda _settings: debrief_model())

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.INGESTION_STATUS_CORRUPT)
    ) as caught:
        await run()

    assert caught.value.code is ErrorCode.INGESTION_STATUS_CORRUPT
    assert not (path.parent / "debrief.json").exists()
    assert status_path(path).read_text(encoding="utf-8") == "{corrupt"
    rows = [
        json.loads(line)
        for line in journal_path(settings.paths.journal_dir, SessionId("debrief-test"))
        .read_text()
        .splitlines()
        if line.strip()
    ]
    [row] = [row for row in rows if row["event"] == "ingestion_status_rejected"]
    assert row["error_type"] == "ValidationError"
