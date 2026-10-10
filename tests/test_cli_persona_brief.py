import asyncio
import json
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ToolErrorMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import SecretStr

import fakeyou.cli.join as cli_join
import fakeyou.cli.main as cli_main
import fakeyou.cli.memory as cli_memory
import fakeyou.memory.memory as memory_module
import fakeyou.orchestration.assembly as assembly
import fakeyou.orchestration.providers as providers
from fakeyou.audio.playback import PlaybackEvent
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli._operator import OperatorIdentity
from fakeyou.config.settings import (
    DEFAULTS,
    Settings,
)
from fakeyou.foundation.greetings import create_greeting_middleware
from fakeyou.memory.persona import PersonaProfile, build_system_prompt, persona_digest
from fakeyou.observability.journal import journal_path
from fakeyou.orchestration.assembly import AgentWiring, LoopBehavior
from fakeyou.persistence.brief import (
    MAX_BRIEF_BYTES,
    brief_digest,
    read_meeting_context,
)
from fakeyou.persistence.record import load_meeting_record, meeting_record_path
from tests._cli_support import (
    BRIEF_DATA,
    BRIEF_FACT,
    BRIEF_TEXTS,
    MEET_LINK,
    PERSONA_DATA,
    PRIVATE_MARKER,
    _enable_audio_platform,
    _join_persona_conversation,
    _join_with_audio,
    _thread_exists,
    expected_context,
    fake_persona_memory,
    forbid_resources,
    meeting_brief,
    meeting_context_file,
    persona_join_arguments,
    printed_join_summary,
    seed_conversation_thread,
    write_brief,
    write_ingest_windows,
    write_persona,
    write_record,
)
from tests.fakes import (
    FakeReplyAudio,
    ToolCallingFakeModel,
    make_graphiti,
    recall_call,
)


def recall_then_reply(reply: str) -> ToolCallingFakeModel:
    return ToolCallingFakeModel(
        responses=[
            recall_call({"query": "Orchid launch"}, id="call-1"),
            AIMessage(content=reply),
        ]
    )


def meeting_files(settings: Settings) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for root in (settings.paths.record_dir, settings.paths.journal_dir)
        if root.exists()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def journal_rows(settings: Settings, meeting_id: str = "meeting-9") -> list[dict]:
    path = journal_path(settings.paths.journal_dir, meeting_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def prepare_brief_join(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)


async def test_join_persona_requires_converse(join, settings, tmp_path: Path) -> None:
    with pytest.raises(FakeYouError) as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                audio_enabled=False,
                conversation_enabled=False,
                persona_path=write_persona(tmp_path),
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_PERSONA_REQUIRES_CONVERSE


async def test_join_memory_requires_persona(join, settings, tmp_path: Path, monkeypatch) -> None:
    forbid_resources(monkeypatch)
    with pytest.raises(FakeYouError) as caught:
        await join(MEET_LINK, **persona_join_arguments(tmp_path, settings, memory=True))
    assert caught.value.code is ErrorCode.MEETING_MEMORY_REQUIRES_PERSONA


@pytest.mark.parametrize(
    "overrides", [{"owner_id": "other-owner"}, {"twin_id": "other-twin"}, {"unknown": "field"}]
)
async def test_join_rejects_invalid_persona_before_resources(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict
) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises((FakeYouError, ValueError), match="owner_id and twin_id|Extra inputs"):
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                memory=True,
                persona_path=write_persona(tmp_path, **overrides),
            ),
        )
    assert not settings.paths.journal_dir.exists()


@pytest.mark.parametrize("overrides", [{"owner_id": "other-owner"}, {"twin_id": "other-twin"}])
async def test_join_reports_a_foreign_persona_identity_with_its_code(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict
) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises(FakeYouError, match="owner_id and twin_id") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                memory=True,
                persona_path=write_persona(tmp_path, **overrides),
            ),
        )
    assert caught.value.code is ErrorCode.PERSONA_IDENTITY_MISMATCH
    assert not settings.paths.journal_dir.exists()


async def test_join_memory_rejects_persona_without_recall_permission(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises(FakeYouError) as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                memory=True,
                persona_path=write_persona(tmp_path, allowed_tools=[]),
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_MEMORY_REQUIRES_RECALL_OR_INGEST


async def test_join_rejects_unreadable_persona(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises(FakeYouError) as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(tmp_path, settings, persona_path=tmp_path / "missing.json"),
        )
    assert caught.value.code is ErrorCode.MEMORY_PERSONA_UNREADABLE


@pytest.mark.parametrize("twin_name,expected", [(None, "Rani Twin"), ("Override", "Override")])
async def test_join_persona_display_name_yields_to_explicit_twin_name(
    session_run,
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    twin_name: str | None,
    expected: str,
) -> None:
    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *_args, **_kwargs: object())
    captured = await _join_with_audio(
        tmp_path,
        monkeypatch,
        conversation_enabled=True,
        twin_name=twin_name,
        persona_path=write_persona(tmp_path),
        settings=settings,
        join=join,
        session_run=session_run,
    )

    assert captured["config"].twin_name == expected
    assert (captured["config"].owner_id, captured["config"].twin_id) == (
        "settings-owner",
        "settings-twin",
    )


async def test_join_persona_without_memory_uses_profile_prompt_without_tools(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Hello.")]),
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert created["persona"] == build_system_prompt(
        profile.model_copy(update={"allowed_tools": frozenset()})
    )
    assert "recall_meeting_context" not in created["persona"]
    assert created["tools"] == []
    assert [type(item) for item in created["middleware"]] == [
        type(create_greeting_middleware("id"))
    ]
    assert audio.spoken == ["Hello."]


async def test_join_persona_memory_exposes_only_profile_scoped_recall(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.memory.recall.strategy = "tool"
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    fact = "Orchid beta launch is planned for 14 October 2026"
    graphiti = make_graphiti(facts=(fact,), episodes={fact: ["meet-1"]})
    model = recall_then_reply("Orchid launches on 14 October.")
    episode = SimpleNamespace(
        uuid="meet-1",
        group_id=profile.memory_group,
        source_description="Google Meet meeting-9 transcript",
        valid_at=datetime(2026, 9, 30, 9, tzinfo=UTC),
        created_at=datetime(2026, 9, 30, 9, tzinfo=UTC),
    )

    async def get_by_uuids(driver, uuids):
        assert driver is graphiti.driver
        return [episode for uuid in uuids if uuid == "meet-1"]

    monkeypatch.setattr(memory_module.EpisodicNode, "get_by_uuids", get_by_uuids)

    created, audio, outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    assert [tool.name for tool in created["tools"]] == ["recall_meeting_context"]
    greeting_type = type(create_greeting_middleware("id"))
    assert [type(item) for item in created["middleware"]] == [
        greeting_type,
        ToolErrorMiddleware,
    ]
    assert graphiti.searched == [
        {
            "query": "Orchid launch",
            "group_ids": [profile.memory_group],
            "num_results": DEFAULTS.memory.recall.search_limit,
            "search_filter": memory_module.CURRENT_FACTS_FILTER,
        },
        {
            "query": "Orchid launch",
            "group_ids": [profile.memory_group],
            "num_results": DEFAULTS.memory.recall.search_limit,
        },
    ]
    assert profile.memory_group != settings.memory.store.group
    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    [content] = [m.content for m in tool_results]
    payload = json.loads(content)
    assert payload["schema"] == "memory_evidence/v1"
    assert [item["text"] for item in payload["facts"]] == [fact]
    assert payload["facts"][0]["sources"][0]["episode_id"] == "meet-1"
    assert graphiti.added == []
    assert audio.spoken == ["Orchid launches on 14 October."]
    assert outcome["phase"] == "listening"
    assert graphiti.events == ["verify", "close"]


async def test_join_recall_failure_is_returned_as_unknown_without_failing_conversation(
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.memory.recall.strategy = "tool"
    graphiti = make_graphiti(search_error=RuntimeError("neo4j went away"))
    model = recall_then_reply("I don't know yet, I'll check with Rani.")

    _created, audio, outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert [m.content for m in tool_results] == [memory_module.RECALL_UNAVAILABLE_RESULT]
    assert audio.spoken == ["I don't know yet, I'll check with Rani."]
    assert outcome["phase"] == "listening"
    outcome["conversation"].raise_if_failed()
    faults = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith('{"event":"fault_recorded"')
    ]
    assert [(fault["source"], fault["kind"]) for fault in faults] == [("memory", "unknown")]
    assert "neo4j went away" not in json.dumps(faults)


async def test_join_stop_during_recall_cancels_without_late_reply(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = make_graphiti(search_gate=asyncio.Event())
    model = recall_then_reply("A late answer.")

    _created, audio, outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settle=graphiti.search_started.wait,
        settings=settings,
        join=join,
    )

    assert graphiti.search_cancelled.is_set()
    assert audio.spoken == []
    assert outcome["conversation"].phase == "closed"
    assert graphiti.events[-1] == "close"


async def test_join_persona_reply_keeps_two_sentence_cap(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ToolCallingFakeModel(
        responses=[AIMessage(content="Sentence one. Sentence two. Sentence three.")]
    )

    _created, audio, _outcome = await _join_persona_conversation(
        tmp_path, monkeypatch, model=model, settings=settings, join=join
    )

    assert audio.spoken == ["Sentence one. Sentence two."]


def test_join_persona_flag_reaches_join(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_join(link, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "join_meeting", fake_join)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "join", MEET_LINK, "--audio", "--converse", "--persona", "p.json", "--memory"],
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    assert captured["options"].identity.persona_path == Path("p.json")
    assert captured["options"].memory is True
    assert captured["options"].brief_path is None


def test_join_brief_flag_reaches_join(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_join(link, **kwargs):
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


async def test_join_ingest_only_persona_defers_memory_without_recall_tool(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.ingestion_status import read_status, status_path

    graphiti = make_graphiti()

    created, _audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Okay.")]),
        graphiti=graphiti,
        memory=True,
        persona_overrides={"ingest_transcripts": True, "allowed_tools": []},
        settings=settings,
        join=join,
    )

    assert created["tools"] == []
    assert [type(item) for item in created["middleware"]] == [
        type(create_greeting_middleware("id"))
    ]
    assert graphiti.added == []
    assert "recall" not in created["persona"].casefold()
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.status == "pending"

    fake_persona_memory(monkeypatch, graphiti)
    await cli_memory.ingest_recorded_meeting(
        meeting_id="meeting-9",
        identity=OperatorIdentity(
            write_persona(tmp_path, ingest_transcripts=True, allowed_tools=[]), None, None
        ),
        twin_name=None,
        settings=settings,
    )

    assert len(graphiti.added) == 1


@pytest.mark.parametrize(
    "overrides,match,code",
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
            "--brief requires an explicit --meeting-id",
            ErrorCode.MEETING_BRIEF_REQUIRES_MEETING_ID,
        ),
    ],
)
async def test_join_brief_requires_converse_persona_and_explicit_meeting_id(
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict,
    match: str,
    code: ErrorCode,
) -> None:
    forbid_resources(monkeypatch)
    arguments = persona_join_arguments(
        tmp_path, settings, persona_path=write_persona(tmp_path), brief_path=write_brief(tmp_path)
    )
    arguments.update(overrides)

    with pytest.raises(FakeYouError, match=match) as caught:
        await join(MEET_LINK, **arguments)
    assert caught.value.code is code
    assert not settings.paths.journal_dir.exists()
    assert not settings.paths.record_dir.exists()


async def test_join_brief_only_refuses_memory_before_resources(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(FakeYouError, match="--memory.*brief_only") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
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
    "content,match,code",
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
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: dict | str | None,
    match: str,
    code: ErrorCode,
) -> None:
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
                tmp_path, settings, persona_path=write_persona(tmp_path), brief_path=brief_path
            ),
        )
    assert caught.value.code is code
    assert PRIVATE_MARKER not in str(caught.value)
    assert all(text not in str(caught.value) for text in BRIEF_TEXTS)
    assert not settings.paths.journal_dir.exists()
    assert not settings.paths.record_dir.exists()


async def test_join_brief_only_snapshots_context_before_the_agent_runs(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    context_file = meeting_context_file(settings)
    observed: list[bool] = []

    class SnapshotObservingModel(ToolCallingFakeModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            observed.append(context_file.exists())
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = SnapshotObservingModel(responses=[AIMessage(content="Hello.")])
    created, audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        brief_path=write_brief(tmp_path),
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    brief = meeting_brief()
    assert observed == [True]
    assert created["tools"] == []
    assert [type(item) for item in created["middleware"]] == [
        type(create_greeting_middleware("id"))
    ]
    assert created["persona"] == build_system_prompt(profile, brief=brief)
    assert model.seen[0][0].content == created["persona"]
    assert audio.spoken == ["Hello."]
    assert read_meeting_context(context_file) == expected_context(brief, profile)
    printed = printed_join_summary(capsys)
    assert printed["brief_sha256"] == brief_digest(brief)
    assert printed["context_path"] == str(context_file)
    assert printed["session_outcome"] == "completed"


async def test_join_persona_memory_brief_exposes_only_profile_scoped_recall(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.memory.recall.strategy = "tool"
    graphiti = make_graphiti()

    created, audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Okay.")]),
        graphiti=graphiti,
        memory=True,
        brief_path=write_brief(tmp_path, knowledge_scope="persona_memory"),
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    brief = meeting_brief(knowledge_scope="persona_memory")
    assert [tool.name for tool in created["tools"]] == ["recall_meeting_context"]
    assert created["persona"] == build_system_prompt(profile, brief=brief)
    assert "recall_meeting_context" in created["persona"]
    assert audio.spoken == ["Okay."]
    assert graphiti.events == ["verify", "close"]
    assert read_meeting_context(meeting_context_file(settings)) == expected_context(brief, profile)


async def test_join_persona_memory_brief_without_memory_omits_recall_rules(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, _audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Okay.")]),
        brief_path=write_brief(tmp_path, knowledge_scope="persona_memory"),
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    brief = meeting_brief(knowledge_scope="persona_memory")
    assert created["tools"] == []
    assert created["persona"] == build_system_prompt(
        profile.model_copy(update={"allowed_tools": frozenset()}), brief=brief
    )
    assert "recall_meeting_context" not in created["persona"]
    assert read_meeting_context(meeting_context_file(settings)) == expected_context(brief, profile)


@pytest.mark.parametrize("reformatted", [False, True])
async def test_join_brief_resume_reuses_the_matching_context_unchanged(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reformatted: bool
) -> None:
    brief_path = write_brief(tmp_path)
    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Hello.")]),
        brief_path=brief_path,
        settings=settings,
        join=join,
    )
    context_file = meeting_context_file(settings)
    recorded = context_file.read_bytes()
    if reformatted:
        reordered = dict(reversed(list(BRIEF_DATA.items())))
        reordered["permissions"] = list(reversed(BRIEF_DATA["permissions"]))
        brief_path.write_text(json.dumps(reordered, indent=4), encoding="utf-8")

    _created, audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Again.")]),
        brief_path=brief_path,
        settings=settings,
        join=join,
    )

    assert audio.spoken == ["Again."]
    assert context_file.read_bytes() == recorded
    reused = [row["reused"] for row in journal_rows(settings) if row["event"] == "brief_recorded"]
    assert reused == [False, True]


@pytest.mark.parametrize(
    "change,match,code",
    [
        ("persona", "meeting context differs.*new meeting ID", ErrorCode.MEETING_CONTEXT_DIFFERS),
        ("brief", "meeting context differs.*new meeting ID", ErrorCode.MEETING_CONTEXT_DIFFERS),
        (
            "without_brief",
            "meeting-9 was started with --brief",
            ErrorCode.MEETING_STARTED_WITH_BRIEF,
        ),
    ],
)
async def test_join_rejects_a_changed_context_without_touching_meeting_files(
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    match: str,
    code: ErrorCode,
) -> None:
    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Hello.")]),
        brief_path=write_brief(tmp_path),
        persona_overrides={"ingest_transcripts": True},
        settings=settings,
        join=join,
    )
    before = meeting_files(settings)
    forbid_resources(monkeypatch)
    arguments = persona_join_arguments(
        tmp_path,
        settings,
        persona_path=write_persona(tmp_path, ingest_transcripts=True),
        brief_path=write_brief(tmp_path),
    )
    if change == "persona":
        arguments["persona_path"] = write_persona(
            tmp_path, ingest_transcripts=True, speaking_style="Formal and slow"
        )
    elif change == "brief":
        arguments["brief_path"] = write_brief(tmp_path, "other-brief.json", questions=[])
    else:
        arguments["brief_path"] = None

    with pytest.raises(FakeYouError, match=match) as caught:
        await join(MEET_LINK, **arguments)
    assert caught.value.code is code
    assert meeting_files(settings) == before


async def test_join_keeps_the_snapshot_when_the_source_brief_changes(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief_path = write_brief(tmp_path)
    edited = [{"id": "demo-date", "text": "EDITED-FACT-MARKER"}]
    model = ToolCallingFakeModel(responses=[AIMessage(content="Hello.")])

    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        brief_path=brief_path,
        on_start=lambda: write_brief(tmp_path, approved_facts=edited),
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    original = meeting_brief()
    assert "EDITED-FACT-MARKER" in brief_path.read_text(encoding="utf-8")
    assert model.seen[0][0].content == build_system_prompt(profile, brief=original)
    assert "EDITED-FACT-MARKER" not in model.seen[0][0].content
    assert read_meeting_context(meeting_context_file(settings)) == expected_context(
        original, profile
    )
    forbid_resources(monkeypatch)
    with pytest.raises(FakeYouError, match="meeting context differs") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path, settings, persona_path=write_persona(tmp_path), brief_path=brief_path
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_CONTEXT_DIFFERS


async def test_join_brief_refuses_a_record_with_conversation_and_no_context(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_ingest_windows(settings, "meeting-9", 1)
    before = meeting_files(settings)
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(FakeYouError, match="conversation data.*new meeting ID") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path),
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_HAS_CONVERSATION_WITHOUT_CONTEXT
    assert meeting_files(settings) == before
    assert not meeting_context_file(settings).exists()


async def test_join_brief_refuses_a_checkpoint_thread_without_context(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread_id = await seed_conversation_thread(settings, "meeting-9")
    other_thread = await seed_conversation_thread(settings, "meeting-10")
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(FakeYouError, match="conversation data.*new meeting ID") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path),
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_HAS_CONVERSATION_WITHOUT_CONTEXT
    assert await _thread_exists(settings, thread_id)
    assert await _thread_exists(settings, other_thread)
    assert meeting_files(settings) == {}
    assert not settings.paths.journal_dir.exists()


@pytest.mark.parametrize("with_brief", [True, False])
async def test_join_refuses_a_corrupt_meeting_context_and_keeps_it(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_brief: bool
) -> None:
    context_file = meeting_context_file(settings)
    context_file.parent.mkdir(parents=True)
    context_file.write_text("{corrupt", encoding="utf-8")
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(FakeYouError, match="meeting context .*corrupt") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path) if with_brief else None,
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_CONTEXT_UNREADABLE
    assert meeting_files(settings) == {"meeting-9/meeting-context.json": b"{corrupt"}
    assert not settings.paths.journal_dir.exists()


async def test_join_brief_refuses_an_unreachable_checkpoint_before_resources(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def unreachable(url):
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE)
        yield

    monkeypatch.setattr(cli_join, "open_checkpointer", unreachable)
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(FakeYouError) as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path),
            ),
        )
    unreachable_message = str(FakeYouError(ErrorCode.DATABASE_UNREACHABLE))
    assert caught.value.code is ErrorCode.MEETING_CONVERSATION_CHECKPOINT_UNCHECKABLE
    assert str(caught.value) == (
        f"conversation checkpoint cannot be checked: {unreachable_message}"
    )
    assert meeting_files(settings) == {}
    assert not settings.paths.journal_dir.exists()


async def test_join_brief_snapshot_write_failure_starts_nothing(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    prepare_brief_join(settings, monkeypatch)

    def reject_replace(source, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", reject_replace)

    with pytest.raises(FakeYouError, match="meeting context not written") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path),
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_CONTEXT_NOT_WRITTEN
    assert meeting_files(settings) == {}
    assert not settings.paths.journal_dir.exists()


async def test_join_journals_brief_metadata_without_brief_text(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Hello.")]),
        brief_path=write_brief(tmp_path),
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    brief = meeting_brief()
    rows = journal_rows(settings)
    [row] = [row for row in rows if row["event"] == "brief_recorded"]
    assert {key: value for key, value in row.items() if key != "at"} == {
        "event": "brief_recorded",
        "meeting_id": "meeting-9",
        "source": "cli",
        "brief_sha256": brief_digest(brief),
        "persona_sha256": persona_digest(profile),
        "knowledge_scope": "brief_only",
        "permissions": ["ask_brief_questions", "explain_facts"],
        "agenda_count": 1,
        "objective_count": 1,
        "approved_fact_count": 1,
        "question_count": 1,
        "objective_ids": ["blockers"],
        "question_ids": ["vendor-needs"],
        "reused": False,
    }
    journal_text = (settings.paths.journal_dir / "meeting-9.jsonl").read_text(encoding="utf-8")
    printed = capsys.readouterr().out
    for text in BRIEF_TEXTS:
        assert text not in journal_text
        assert text not in printed


async def test_join_briefs_stay_isolated_per_meeting_and_resume_in_one_checkpointer(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second_fact = "The synthetic vendor budget stays fixed."
    second = {
        "meeting_id": "meeting-10",
        "approved_facts": [{"id": "vendor-budget", "text": second_fact}],
    }
    first_model = ToolCallingFakeModel(responses=[AIMessage(content="First.")])
    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=first_model,
        brief_path=write_brief(tmp_path),
        transcripts=("First meeting question?",),
        native_checkpointer=True,
        settings=settings,
        join=join,
    )
    second_model = ToolCallingFakeModel(responses=[AIMessage(content="Second.")])
    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=second_model,
        meeting_id="meeting-10",
        brief_path=write_brief(tmp_path, "second-brief.json", **second),
        transcripts=("Second meeting question?",),
        native_checkpointer=True,
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    second_prompt = second_model.seen[0][0].content
    assert second_prompt == build_system_prompt(profile, brief=meeting_brief(**second))
    assert BRIEF_FACT not in second_prompt
    assert second_fact not in first_model.seen[0][0].content
    second_history = " ".join(str(message.content) for message in second_model.seen[0])
    assert "First meeting question?" not in second_history
    assert read_meeting_context(meeting_context_file(settings)) == expected_context(
        meeting_brief(), profile
    )
    assert read_meeting_context(meeting_context_file(settings, "meeting-10")) == (
        expected_context(meeting_brief(**second), profile)
    )

    retry_model = ToolCallingFakeModel(responses=[AIMessage(content="Retry.")])
    _created, audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=retry_model,
        brief_path=write_brief(tmp_path),
        transcripts=("Retry question?",),
        native_checkpointer=True,
        settings=settings,
        join=join,
    )

    assert audio.spoken == ["Retry."]
    retry_history = " ".join(str(message.content) for message in retry_model.seen[0])
    assert "First meeting question?" in retry_history
    assert "Second meeting question?" not in retry_history
    assert retry_model.seen[0][0].content == build_system_prompt(profile, brief=meeting_brief())


async def test_join_brief_stop_before_pcm_cancels_the_model_turn(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    class HangingModel(ToolCallingFakeModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            started.set()
            await asyncio.Event().wait()
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    _created, audio, outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=HangingModel(responses=[AIMessage(content="A late answer.")]),
        brief_path=write_brief(tmp_path),
        settle=started.wait,
        settings=settings,
        join=join,
    )

    assert audio.spoken == []
    assert outcome["conversation"].phase == "closed"
    rows = journal_rows(settings)
    assert [row["reason"] for row in rows if row["event"] == "agent_cancelled"] == ["close"]
    assert not [row for row in rows if row["event"] == "playback_finished"]
    entries = load_meeting_record(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    assert [entry.kind for entry in entries] == ["transcript"]
    assert read_meeting_context(meeting_context_file(settings)) is not None


async def test_join_brief_stop_during_playback_records_no_twin_speech(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BlockingAudio(FakeReplyAudio):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()

        async def speak(self, text, *, sentence_index=None, transcript_id=None):
            self.speaking = True
            self.on_playback_event(PlaybackEvent("playback_started", text))
            self.entered.set()
            await asyncio.Event().wait()

    blocking = BlockingAudio()
    _created, audio, outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="A long answer.")]),
        brief_path=write_brief(tmp_path),
        audio=blocking,
        settle=blocking.entered.wait,
        settings=settings,
        join=join,
    )

    assert audio is blocking
    assert audio.spoken == []
    assert outcome["conversation"].phase == "closed"
    playback = [row["event"] for row in journal_rows(settings) if row["source"] == "playback"]
    assert playback == ["playback_started"]
    entries = load_meeting_record(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    assert [entry.kind for entry in entries] == ["transcript"]
    assert read_meeting_context(meeting_context_file(settings)) is not None


async def test_open_conversation_rejects_a_brief_without_a_persona(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        providers, "init_chat_model", lambda *_a, **_k: pytest.fail("model must stay closed")
    )
    monkeypatch.setattr(
        assembly, "open_checkpointer", lambda *_a, **_k: pytest.fail("saver must stay closed")
    )
    config = SimpleNamespace(owner_id="settings-owner", twin_id="settings-twin", meeting_id="m")

    with pytest.raises(FakeYouError) as caught:
        async with assembly.open_conversation(
            settings,
            config,
            agent=AgentWiring(profile=None, brief=meeting_brief(), graphiti=None),
            behavior=LoopBehavior(on_transition=lambda *_args: None),
        ):
            pytest.fail("conversation must not open")
    assert caught.value.code is ErrorCode.MEETING_BRIEF_REQUIRES_PROFILE


async def test_open_conversation_rejects_memory_with_a_brief_only_scope(
    settings, monkeypatch: pytest.MonkeyPatch
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
            config,
            agent=AgentWiring(
                profile=PersonaProfile.model_validate(PERSONA_DATA),
                brief=meeting_brief(),
                graphiti=make_graphiti(),
            ),
            behavior=LoopBehavior(on_transition=lambda *_args: None),
        ):
            pytest.fail("conversation must not open")
    assert caught.value.code is ErrorCode.MEETING_MEMORY_NOT_ALLOWED_BRIEF_ONLY


async def test_join_hung_recall_times_out_as_unknown_and_keeps_listening(
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.memory.recall.strategy = "tool"
    graphiti = make_graphiti(search_gate=asyncio.Event())
    model = recall_then_reply("I don't know yet, I'll check with Rani.")
    settings.memory.recall.timeout_seconds = 0.01

    _created, audio, outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert [m.content for m in tool_results] == [memory_module.RECALL_UNAVAILABLE_RESULT]
    assert graphiti.search_cancelled.is_set()
    assert audio.spoken == ["I don't know yet, I'll check with Rani."]
    assert outcome["phase"] == "listening"
    faults = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if line.startswith('{"event":"fault_recorded"')
    ]
    assert [(row["source"], row["kind"]) for row in faults] == [("memory", "timeout")]


async def test_memory_ingest_command_ingests_the_record_into_the_persona_group(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)

    await cli_memory.ingest_recorded_meeting(
        meeting_id="meeting-9",
        identity=OperatorIdentity(write_persona(tmp_path, vocabulary=["Orchid"]), None, None),
        twin_name=None,
        settings=settings,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    [episode] = graphiti.added
    assert episode["group_id"] == profile.memory_group
    assert "Orchid" in episode["custom_extraction_instructions"]
    assert "'Rani Twin'" in episode["custom_extraction_instructions"]
    summary = json.loads(capsys.readouterr().out)
    assert (summary["meeting_id"], summary["ingested"], summary["completed"]) == (
        "meeting-9",
        1,
        True,
    )
    journal = (settings.paths.journal_dir / "meeting-9.jsonl").read_text()
    assert '"event":"memory_ingest_finished"' in journal


async def test_join_passes_persona_vocabulary_as_speech_keywords(
    session_run, join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *_args, **_kwargs: object())
    captured = await _join_with_audio(
        tmp_path,
        monkeypatch,
        conversation_enabled=True,
        persona_path=write_persona(tmp_path, vocabulary=["Orchid", "Budi"]),
        settings=settings,
        join=join,
        session_run=session_run,
    )

    from fakeyou.memory.persona import number_keywords

    assert captured["open_audio"].keywords["policy"].speech.stt_keywords == (
        "Rani Twin",
        "Orchid",
        "Budi",
        *number_keywords("id"),
    )


async def test_join_without_persona_has_no_speech_keywords(
    session_run, join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = await _join_with_audio(
        tmp_path, monkeypatch, settings=settings, join=join, session_run=session_run
    )

    assert captured["open_audio"].keywords["policy"].speech.stt_keywords is None


async def test_join_rejects_persona_language_mismatch_before_resources(
    join, settings, tmp_path, monkeypatch
):
    _enable_audio_platform(monkeypatch)
    settings.stt.api_key = SecretStr("muse-key")
    forbid_resources(monkeypatch)
    persona = write_persona(tmp_path, language="en")
    with pytest.raises(FakeYouError, match="persona language") as caught:
        await join(MEET_LINK, audio_enabled=True, conversation_enabled=True, persona_path=persona)
    assert caught.value.code is ErrorCode.MEETING_PERSONA_LANGUAGE_MISMATCH
    assert not settings.paths.journal_dir.exists()


async def test_join_without_persona_writes_no_status(
    session_run, join, settings, tmp_path: Path, capsys
) -> None:
    from fakeyou.persistence.ingestion_status import status_path

    await join(MEET_LINK)

    record_path = meeting_record_path(settings.paths.record_dir, "meeting-1")
    assert not status_path(record_path).exists()
    printed = printed_join_summary(capsys)
    assert printed["session_outcome"] == "completed"
    assert printed["ingestion_outcome"] == "not_requested"
    assert printed["record_error"] is None
    assert printed["resume_command"] is None
