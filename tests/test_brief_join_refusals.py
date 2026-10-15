import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import fakeyou.cli.join as cli_join
import fakeyou.cli.main as cli_main
import fakeyou.orchestration.assembly as assembly
import fakeyou.orchestration.providers as providers
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.memory.persona import PersonaProfile
from fakeyou.orchestration.assembly import AgentWiring, LoopBehavior
from fakeyou.persistence.brief import MAX_BRIEF_BYTES
from tests._cli_support import (
    BRIEF_DATA,
    BRIEF_TEXTS,
    MEET_LINK,
    PERSONA_DATA,
    PRIVATE_MARKER,
    forbid_resources,
    meeting_brief,
    meeting_context_file,
    persona_join_arguments,
    seed_conversation_thread,
    thread_exists,
    write_brief,
    write_ingest_windows,
    write_persona,
)
from tests._error_support import message_pattern
from tests._memory_fakes import make_graphiti
from tests._persona_brief_support import meeting_files, prepare_brief_join


def test_join_brief_flag_reaches_join(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_join(link: str, **kwargs: Any) -> None:
        del link
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "join_meeting", fake_join)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fakeyou",
            "join",
            MEET_LINK,
            "--audio",
            "--converse",
            "--persona",
            "p.json",
            "--meeting-id",
            "meeting-9",
            "--brief",
            "b.json",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    assert captured["options"].brief_path == Path("b.json")
    assert captured["options"].meeting_id == "meeting-9"


@pytest.mark.parametrize(
    "case",
    [
        (
            {"audio_enabled": False, "conversation_enabled": False, "persona_path": None},
            "--brief requires --converse",
            ErrorCode.MEETING_BRIEF_REQUIRES_CONVERSE,
        ),
        (
            {"persona_path": None},
            "--brief requires --persona",
            ErrorCode.MEETING_BRIEF_REQUIRES_PERSONA,
        ),
        (
            {"meeting_id": None},
            "--brief requires an explicit --meeting-id",
            ErrorCode.MEETING_BRIEF_REQUIRES_MEETING_ID,
        ),
        (
            {"meeting_id": ""},
            "--meeting-id",
            ErrorCode.SESSION_ID_INVALID,
        ),
    ],
)
async def test_join_brief_requires_converse_persona_and_explicit_meeting_id(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[dict[str, Any], str, ErrorCode],
) -> None:
    overrides, match, code = case
    forbid_resources(monkeypatch)
    arguments = persona_join_arguments(
        settings, persona_path=write_persona(tmp_path), brief_path=write_brief(tmp_path)
    )
    arguments.update(overrides)

    with pytest.raises(FakeYouError, match=match) as caught:
        await join(MEET_LINK, **arguments)
    assert caught.value.code is code
    assert not settings.paths.journal_dir.exists()
    assert not settings.paths.record_dir.exists()


async def test_join_brief_only_refuses_memory_before_resources(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(FakeYouError, match="--memory.*brief_only") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                settings,
                memory=True,
                persona_path=write_persona(tmp_path),
                brief_path=write_brief(tmp_path),
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_MEMORY_NOT_ALLOWED_BRIEF_ONLY
    assert not settings.paths.journal_dir.exists()
    assert not settings.paths.record_dir.exists()


@pytest.mark.parametrize(
    "case",
    [
        (None, "--brief cannot be read", ErrorCode.MEETING_BRIEF_UNREADABLE),
        (
            "oversize",
            "meeting brief must be at most",
            ErrorCode.MEETING_BRIEF_TOO_LARGE,
        ),
        (
            {"owner_id": "other-owner"},
            "meeting brief .*must match the session",
            ErrorCode.MEETING_BRIEF_SCOPE_MISMATCH,
        ),
        (
            {"twin_id": "other-twin"},
            "meeting brief .*must match the session",
            ErrorCode.MEETING_BRIEF_SCOPE_MISMATCH,
        ),
        (
            {"meeting_id": "meeting-10"},
            "meeting brief .*must match the session",
            ErrorCode.MEETING_BRIEF_SCOPE_MISMATCH,
        ),
        (
            {"approved_facts": [{"id": "secret", "text": PRIVATE_MARKER * 40}]},
            "--brief is invalid",
            ErrorCode.MEETING_BRIEF_SCHEMA_INVALID,
        ),
        (
            {"permissions": [f"approve_{PRIVATE_MARKER}"]},
            "--brief is invalid",
            ErrorCode.MEETING_BRIEF_SCHEMA_INVALID,
        ),
        (
            {PRIVATE_MARKER: "unknown key"},
            r"--brief is invalid: 1 validation error\(s\): extra_forbidden$",
            ErrorCode.MEETING_BRIEF_SCHEMA_INVALID,
        ),
    ],
)
async def test_join_rejects_an_unusable_brief_before_resources(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[dict[str, Any] | str | None, str, ErrorCode],
) -> None:
    content, match, code = case
    prepare_brief_join(settings, monkeypatch)
    if content is None:
        brief_path = tmp_path / "missing-brief.json"
    elif content == "oversize":
        brief_path = tmp_path / "oversize-brief.json"
        brief_path.write_text(
            json.dumps(BRIEF_DATA) + " " * MAX_BRIEF_BYTES + PRIVATE_MARKER, encoding="utf-8"
        )
    else:
        assert isinstance(content, dict)
        brief_path = write_brief(tmp_path, **content)

    with pytest.raises(FakeYouError, match=match) as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                settings, persona_path=write_persona(tmp_path), brief_path=brief_path
            ),
        )
    assert caught.value.code is code
    assert PRIVATE_MARKER not in str(caught.value)
    assert all(text not in str(caught.value) for text in BRIEF_TEXTS)
    assert not settings.paths.journal_dir.exists()
    assert not settings.paths.record_dir.exists()


async def test_join_brief_refuses_a_record_with_conversation_and_no_context(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_ingest_windows(settings, "meeting-9", 1)
    before = meeting_files(settings)
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(FakeYouError, match="conversation data.*new meeting ID") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path),
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_HAS_CONVERSATION_WITHOUT_CONTEXT
    assert meeting_files(settings) == before
    assert not meeting_context_file(settings).exists()


async def test_join_brief_refuses_a_checkpoint_thread_without_context(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread_id = await seed_conversation_thread(settings, "meeting-9")
    other_thread = await seed_conversation_thread(settings, "meeting-10")
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(FakeYouError, match="conversation data.*new meeting ID") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path),
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_HAS_CONVERSATION_WITHOUT_CONTEXT
    assert await thread_exists(settings, thread_id)
    assert await thread_exists(settings, other_thread)
    assert meeting_files(settings) == {}
    assert not settings.paths.journal_dir.exists()


@pytest.mark.parametrize("with_brief", [True, False])
async def test_join_refuses_a_corrupt_meeting_context_and_keeps_it(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_brief: bool,
) -> None:
    context_file = meeting_context_file(settings)
    context_file.parent.mkdir(parents=True)
    context_file.write_text("{corrupt", encoding="utf-8")
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(FakeYouError, match="meeting context .*corrupt") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path) if with_brief else None,
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_CONTEXT_UNREADABLE
    assert meeting_files(settings) == {"meeting-9/meeting-context.json": b"{corrupt"}
    assert not settings.paths.journal_dir.exists()


async def test_join_brief_refuses_an_unreachable_checkpoint_before_resources(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def unreachable(url: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        del url
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE)
        yield

    monkeypatch.setattr(cli_join, "open_checkpointer", unreachable)
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEETING_CONVERSATION_CHECKPOINT_UNCHECKABLE)
    ) as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path),
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_CONVERSATION_CHECKPOINT_UNCHECKABLE
    assert str(caught.value) == (
        "conversation checkpoint cannot be checked: FakeYouError: "
        "PostgreSQL is unreachable; check FAKEYOU_DATABASE__URL and that the postgres "
        "service is up."
    )
    assert meeting_files(settings) == {}
    assert not settings.paths.journal_dir.exists()


async def test_open_conversation_rejects_a_brief_without_a_persona(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        providers, "init_chat_model", lambda *_a, **_k: pytest.fail("model must stay closed")
    )
    monkeypatch.setattr(
        assembly, "open_checkpointer", lambda *_a, **_k: pytest.fail("saver must stay closed")
    )
    config = SimpleNamespace(owner_id="settings-owner", twin_id="settings-twin", meeting_id="m")

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEETING_BRIEF_REQUIRES_PROFILE)
    ) as caught:
        async with assembly.open_conversation(
            settings,
            cast(Any, config),
            agent=AgentWiring(profile=None, brief=meeting_brief(), graphiti=None),
            behavior=LoopBehavior(on_transition=lambda *_args: None),
        ):
            pytest.fail("conversation must not open")
    assert caught.value.code is ErrorCode.MEETING_BRIEF_REQUIRES_PROFILE


async def test_open_conversation_rejects_memory_with_a_brief_only_scope(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        providers, "init_chat_model", lambda *_a, **_k: pytest.fail("model must stay closed")
    )
    monkeypatch.setattr(
        assembly, "open_checkpointer", lambda *_a, **_k: pytest.fail("saver must stay closed")
    )
    config = SimpleNamespace(owner_id="settings-owner", twin_id="settings-twin", meeting_id="m")

    with pytest.raises(FakeYouError, match="brief_only") as caught:
        async with assembly.open_conversation(
            settings,
            cast(Any, config),
            agent=AgentWiring(
                profile=PersonaProfile.model_validate(PERSONA_DATA),
                brief=meeting_brief(),
                graphiti=make_graphiti(),
            ),
            behavior=LoopBehavior(on_transition=lambda *_args: None),
        ):
            pytest.fail("conversation must not open")
    assert caught.value.code is ErrorCode.MEETING_MEMORY_NOT_ALLOWED_BRIEF_ONLY
