import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest
from graphiti_core import Graphiti

import fakeyou.cli.main as cli_main
import fakeyou.memory.forget as forget_service
import fakeyou.orchestration.providers as providers
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.output import emit_json
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.conversation import conversation_thread_id
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.db.engine import configured_url, require_url
from fakeyou.memory.forget import ForgetRequest, require_complete, run_forget
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from fakeyou.orchestration.lifecycle import meeting_thread_id
from fakeyou.persistence.brief import context_path, read_meeting_context, write_meeting_context
from fakeyou.persistence.ingestion_status import record_lock
from fakeyou.persistence.lease import LockPurpose
from fakeyou.persistence.record import meeting_record_path
from tests._cli_memory_support import tracked_memory
from tests._cli_support import (
    PERSONA_DATA,
    expected_context,
    fake_persona_memory,
    meeting_brief,
    meeting_context_file,
    seed_conversation_thread,
    thread_exists,
    write_persona,
)
from tests._error_support import message_pattern
from tests._memory_fakes import make_graphiti


async def forget_in_process(
    *,
    identity: OperatorIdentity,
    meeting_id: SessionId | None,
    episode_uuid: str | None,
    settings: Settings,
    keep_record: bool = False,
) -> None:
    summary = await run_forget(
        ForgetRequest(meeting_id, episode_uuid, keep_record),
        access=OperatorAccess(identity, settings, lambda: providers.open_persona_memory(settings)),
        lifecycle_thread_id=meeting_thread_id,
    )
    document: dict[str, Any] = {"removed_episodes": summary.removed_episodes}
    if summary.files is not None:
        document |= asdict(summary.files)
    emit_json(document)
    require_complete(summary)


async def test_memory_forget_meeting_uses_persona_group(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    calls: list[dict[str, Any]] = []

    async def forget(memory: Any, **kwargs: Any) -> int:
        calls.append({"memory": memory, **kwargs})
        return 3

    monkeypatch.setattr(forget_service, "forget_meeting", forget)

    await forget_in_process(
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        meeting_id=SessionId("meeting-9"),
        episode_uuid=None,
        settings=settings,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert calls == [
        {"memory": graphiti, "group_id": profile.memory_group, "meeting_id": "meeting-9"}
    ]
    assert json.loads(capsys.readouterr().out) == {
        "removed_episodes": 3,
        "record_removed": False,
        "journal_files_removed": 0,
        "conversation_thread_removed": False,
        "browser_profile_removed": False,
    }


async def forget_meeting_with_local_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, settings: Settings, keep_record: bool
) -> tuple[Settings, str]:
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}\n")
    for artifact in ("debrief.json", "debrief.md", "debrief-attempt.json"):
        record_path.with_name(artifact).write_text("synthetic artifact")
    write_meeting_context(context_path(record_path), expected_context(meeting_brief(), profile))
    settings.paths.journal_dir.mkdir()
    for name in ("meeting-9.jsonl", "meeting-9.jsonl.1", "meeting-10.jsonl"):
        (settings.paths.journal_dir / name).write_text("{}\n")
    thread_id = await seed_conversation_thread(settings, "meeting-9")
    await seed_conversation_thread(settings, "meeting-10")
    from fakeyou.orchestration.lifecycle import (
        LifecycleThread,
        build_lifecycle_graph,
        initial_lifecycle_state,
        initialize_lifecycle,
        meeting_thread_id,
    )

    async with open_checkpointer(require_url(configured_url(settings))) as saver:
        for meeting in ("meeting-9", "meeting-10"):
            lifecycle = LifecycleThread(
                build_lifecycle_graph(saver),
                meeting_thread_id(profile.twin_id, meeting),
                initial_lifecycle_state(profile.owner_id, profile.twin_id, meeting),
            )
            await initialize_lifecycle(lifecycle, resume=False)
    fake_persona_memory(monkeypatch, make_graphiti())

    async def forget(_memory: Any, **_kwargs: Any) -> int:
        return 2

    monkeypatch.setattr(forget_service, "forget_meeting", forget)
    await forget_in_process(
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        meeting_id=SessionId("meeting-9"),
        episode_uuid=None,
        settings=settings,
        keep_record=keep_record,
    )
    return settings, thread_id


async def test_memory_forget_meeting_removes_record_journal_and_thread(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings, thread_id = await forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=False, settings=settings
    )

    assert json.loads(capsys.readouterr().out) == {
        "removed_episodes": 2,
        "record_removed": True,
        "journal_files_removed": 2,
        "conversation_thread_removed": True,
        "browser_profile_removed": False,
    }
    assert not (settings.paths.record_dir / "meeting-9").exists()
    assert sorted(path.name for path in settings.paths.journal_dir.iterdir()) == [
        "meeting-10.jsonl"
    ]
    assert not await thread_exists(settings, thread_id)
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    other = conversation_thread_id(profile.owner_id, profile.twin_id, "meeting-10")
    assert await thread_exists(settings, other)


async def test_memory_forget_meeting_keep_record_only_removes_episodes(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings, thread_id = await forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=True, settings=settings
    )

    assert json.loads(capsys.readouterr().out) == {"removed_episodes": 2}
    assert meeting_record_path(settings.paths.record_dir, SessionId("meeting-9")).exists()
    assert (settings.paths.journal_dir / "meeting-9.jsonl").exists()
    assert await thread_exists(settings, thread_id)
    for artifact in ("debrief.json", "debrief.md", "debrief-attempt.json"):
        assert (
            meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
            .with_name(artifact)
            .exists()
        )


@pytest.mark.parametrize("keep_record", [False, True])
async def test_memory_forget_keeps_the_meeting_context_only_with_the_record(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, keep_record: bool
) -> None:
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    context_file = meeting_context_file(settings)

    await forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=keep_record, settings=settings
    )

    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    assert record_path.exists() is keep_record
    assert context_file.exists() is keep_record
    if keep_record:
        assert read_meeting_context(context_file) == expected_context(meeting_brief(), profile)


async def test_memory_forget_episode_uses_persona_group(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    calls: list[dict[str, Any]] = []

    async def forget(_memory: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(forget_service, "forget_episode", forget)

    await forget_in_process(
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        meeting_id=None,
        episode_uuid="episode-7",
        settings=settings,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert calls == [{"group_id": profile.memory_group, "episode_uuid": "episode-7"}]


@pytest.mark.parametrize(
    ("meeting_id", "episode_uuid"),
    [(None, None), ("meeting-9", "episode-7")],
)
async def test_memory_forget_validates_before_opening_memory(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    meeting_id: str | None,
    episode_uuid: str | None,
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(FakeYouError, match="exactly one") as caught:
        await forget_in_process(
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            meeting_id=None if meeting_id is None else SessionId(meeting_id),
            episode_uuid=episode_uuid,
            settings=settings,
        )
    assert caught.value.code is ErrorCode.MEMORY_FORGET_TARGET_REQUIRED
    assert opened == []


def test_memory_forget_rejects_a_malformed_meeting_id_before_opening_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())
    persona = write_persona(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "memory-forget", "--meeting-id", "bad meeting", "--persona", str(persona)],
    )

    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == ExitCode.INPUT_REJECTED
    assert "invalid --meeting-id: 'bad meeting'" in capsys.readouterr().err
    assert opened == []


async def test_memory_forget_keep_record_requires_a_meeting(
    settings: Settings, tmp_path: Path
) -> None:
    with pytest.raises(FakeYouError, match="--keep-record") as caught:
        await forget_in_process(
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            meeting_id=None,
            episode_uuid="episode-7",
            settings=settings,
            keep_record=True,
        )
    assert caught.value.code is ErrorCode.MEMORY_KEEP_RECORD_REQUIRES_MEETING


async def test_memory_forget_should_delete_lifecycle_and_preserve_other_meetings(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.orchestration.lifecycle import meeting_thread_id

    settings, thread_id = await forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=False, settings=settings
    )
    profile = PersonaProfile.model_validate(PERSONA_DATA)

    assert not await thread_exists(settings, thread_id)
    assert not await thread_exists(settings, meeting_thread_id(profile.twin_id, "meeting-9"))
    assert await thread_exists(settings, meeting_thread_id(profile.twin_id, "meeting-10"))


@pytest.mark.parametrize("lock_purpose", ["meeting", "ingestion", "debrief"])
@pytest.mark.parametrize("keep_record", [False, True])
async def test_memory_forget_meeting_refuses_an_active_lock(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    keep_record: bool,
    lock_purpose: LockPurpose,
) -> None:
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}\n", encoding="utf-8")
    context = expected_context(meeting_brief(), PersonaProfile.model_validate(PERSONA_DATA))
    write_meeting_context(context_path(record_path), context)
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    async def forget(_memory: Any, **_kwargs: Any) -> NoReturn:
        pytest.fail("provider must stay closed while the record is locked")

    monkeypatch.setattr(forget_service, "forget_meeting", forget)

    with record_lock(record_path, purpose=lock_purpose):
        with pytest.raises(FakeYouError, match="locked") as caught:
            await forget_in_process(
                identity=OperatorIdentity(write_persona(tmp_path), None, None),
                meeting_id=SessionId("meeting-9"),
                episode_uuid=None,
                settings=settings,
                keep_record=keep_record,
            )
    assert caught.value.code is ErrorCode.RECORD_LOCKED
    assert opened == []
    assert record_path.exists()
    assert read_meeting_context(context_path(record_path)) == context


async def test_open_persona_memory_passes_domain_error_through(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def fake_open(_config: object) -> AsyncIterator[object]:
        yield object()

    async def boom(_memory: object) -> None:
        raise FakeYouError(ErrorCode.CONFIG_VISUAL_BASE_URL_REQUIRED)

    monkeypatch.setattr(providers, "open_graphiti_memory", fake_open)
    monkeypatch.setattr(providers, "verify_memory_connection", boom)

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.CONFIG_VISUAL_BASE_URL_REQUIRED),
    ) as caught:
        async with providers.open_persona_memory(settings):
            pass

    assert caught.value.code is ErrorCode.CONFIG_VISUAL_BASE_URL_REQUIRED


async def test_run_forget_refuses_an_ambiguous_target_before_opening_memory(
    settings: Settings, tmp_path: Path
) -> None:
    opened: list[str] = []
    access = OperatorAccess(
        OperatorIdentity(write_persona(tmp_path), None, None),
        settings,
        lambda: tracked_memory(opened),
    )

    with pytest.raises(FakeYouError, match="exactly one") as caught:
        await run_forget(
            ForgetRequest(None, None),
            access=access,
            lifecycle_thread_id=meeting_thread_id,
        )
    assert caught.value.code is ErrorCode.MEMORY_FORGET_TARGET_REQUIRED
    assert opened == []


@asynccontextmanager
async def _memory_context(memory: Graphiti) -> AsyncIterator[Graphiti]:
    yield memory


def _forget_access(settings: Settings, tmp_path: Path) -> OperatorAccess:
    return OperatorAccess(
        OperatorIdentity(write_persona(tmp_path), None, None),
        settings,
        lambda: _memory_context(cast(Graphiti, make_graphiti())),
    )


async def test_second_run_forget_over_the_same_meeting_is_complete(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}\n", encoding="utf-8")
    removed = iter([2, 0])

    async def forget(_memory: Any, **_kwargs: Any) -> int:
        return next(removed)

    monkeypatch.setattr(forget_service, "forget_meeting", forget)
    access = _forget_access(settings, tmp_path)
    request = ForgetRequest(SessionId("meeting-9"), None)

    first = await run_forget(request, access=access, lifecycle_thread_id=meeting_thread_id)
    second = await run_forget(request, access=access, lifecycle_thread_id=meeting_thread_id)

    assert [stage.stage for stage in first.stages] == [
        "graph",
        "record",
        "journal",
        "browser_profile",
        "checkpoint",
    ]
    assert (first.removed_episodes, first.complete) == (2, True)
    assert {stage.stage: stage.status for stage in first.stages}["record"] == "removed"
    assert (second.removed_episodes, second.complete) == (0, True)
    assert {stage.stage: stage.status for stage in second.stages} == {
        "graph": "absent",
        "record": "absent",
        "journal": "absent",
        "browser_profile": "absent",
        "checkpoint": "absent",
    }


async def test_failed_graph_stage_still_runs_the_file_stages(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}\n", encoding="utf-8")

    async def broken(_memory: Any, **_kwargs: Any) -> NoReturn:
        raise RuntimeError("graph is down")

    monkeypatch.setattr(forget_service, "forget_meeting", broken)

    summary = await run_forget(
        ForgetRequest(SessionId("meeting-9"), None),
        access=_forget_access(settings, tmp_path),
        lifecycle_thread_id=meeting_thread_id,
    )

    stages = {stage.stage: stage for stage in summary.stages}
    assert (stages["graph"].status, stages["graph"].detail) == ("failed", "RuntimeError")
    assert stages["record"].status == "removed"
    assert summary.complete is False
    assert summary.removed_episodes == 0
    assert not record_path.exists()


async def test_keep_record_reports_the_file_stages_as_kept(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forget(_memory: Any, **_kwargs: Any) -> int:
        return 1

    monkeypatch.setattr(forget_service, "forget_meeting", forget)

    summary = await run_forget(
        ForgetRequest(SessionId("meeting-9"), None, keep_record=True),
        access=_forget_access(settings, tmp_path),
        lifecycle_thread_id=meeting_thread_id,
    )

    assert summary.files is None
    assert summary.complete is True
    assert {stage.stage: stage.status for stage in summary.stages} == {
        "graph": "removed",
        "record": "kept",
        "journal": "kept",
        "browser_profile": "kept",
        "checkpoint": "kept",
    }


async def test_forget_of_an_absent_episode_is_an_idempotent_summary(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def absent(_memory: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(forget_service, "forget_episode", absent)

    summary = await run_forget(
        ForgetRequest(None, "episode-7"),
        access=_forget_access(settings, tmp_path),
        lifecycle_thread_id=meeting_thread_id,
    )

    assert (summary.removed_episodes, summary.complete) == (0, True)
    assert [(stage.stage, stage.status) for stage in summary.stages] == [("graph", "absent")]


async def test_require_complete_turns_a_failed_stage_into_the_named_error(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(_memory: Any, **_kwargs: Any) -> NoReturn:
        raise RuntimeError("graph is down")

    monkeypatch.setattr(forget_service, "forget_meeting", broken)
    summary = await run_forget(
        ForgetRequest(SessionId("meeting-9"), None, keep_record=True),
        access=_forget_access(settings, tmp_path),
        lifecycle_thread_id=meeting_thread_id,
    )

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEMORY_FORGET_FILES_INCOMPLETE)
    ) as caught:
        require_complete(summary)

    assert caught.value.code is ErrorCode.MEMORY_FORGET_FILES_INCOMPLETE
    assert "graph" in str(caught.value)
    assert "RuntimeError" in str(caught.value)


async def test_require_complete_returns_a_complete_summary(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def forget(_memory: Any, **_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(forget_service, "forget_meeting", forget)
    summary = await run_forget(
        ForgetRequest(SessionId("meeting-9"), None),
        access=_forget_access(settings, tmp_path),
        lifecycle_thread_id=meeting_thread_id,
    )

    assert require_complete(summary) is summary
