import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, override

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.conversation.middleware import (
    ChatTurnToolsMiddleware,
    SpeakerAttributionMiddleware,
)
from fakeyou.foundation.greetings import create_greeting_middleware
from fakeyou.memory.persona import PersonaProfile, build_system_prompt, persona_digest
from fakeyou.perception.context import PERCEPTION_GUIDANCE, PerceptionContextMiddleware
from fakeyou.persistence.brief import brief_digest, load_meeting_brief, read_meeting_context
from tests._cli_support import (
    BRIEF_DATA,
    BRIEF_TEXTS,
    MEET_LINK,
    PERSONA_DATA,
    expected_context,
    forbid_resources,
    join_persona_conversation,
    meeting_brief,
    meeting_context_file,
    persona_join_arguments,
    printed_join_summary,
    write_brief,
    write_persona,
)
from tests._conversation_fakes import ToolCallingFakeModel
from tests._memory_fakes import make_graphiti
from tests._persona_brief_support import journal_rows, meeting_files, prepare_brief_join


async def test_join_brief_only_snapshots_context_before_the_agent_runs(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    context_file = meeting_context_file(settings)
    observed: list[bool] = []

    class SnapshotObservingModel(ToolCallingFakeModel):
        @override
        async def _agenerate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: AsyncCallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            observed.append(context_file.exists())
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = SnapshotObservingModel(responses=[AIMessage(content="Hello.")])
    created, audio, _outcome = await join_persona_conversation(
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
        type(create_greeting_middleware("id")),
        ChatTurnToolsMiddleware,
        PerceptionContextMiddleware,
        SpeakerAttributionMiddleware,
    ]
    assert created["persona"] == (
        f"{build_system_prompt(profile, brief=brief)}\n\n{PERCEPTION_GUIDANCE}"
    )
    assert model.seen[0][0].content == created["persona"]
    assert audio.spoken == ["Hello."]
    assert read_meeting_context(context_file) == expected_context(brief, profile)
    printed = printed_join_summary(capsys)
    assert printed["brief_sha256"] == brief_digest(brief)
    assert printed["context_path"] == str(context_file)
    assert printed["session_outcome"] == "completed"


async def test_join_persona_memory_brief_exposes_only_profile_scoped_recall(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.memory.recall.strategy = "tool"
    graphiti = make_graphiti()

    created, audio, _outcome = await join_persona_conversation(
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
    assert created["persona"] == (
        f"{build_system_prompt(profile, brief=brief)}\n\n{PERCEPTION_GUIDANCE}"
    )
    assert "recall_meeting_context" in created["persona"]
    assert audio.spoken == ["Okay."]
    assert graphiti.events == ["verify", "close"]
    assert read_meeting_context(meeting_context_file(settings)) == expected_context(brief, profile)


async def test_join_persona_memory_brief_without_memory_omits_recall_rules(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, _audio, _outcome = await join_persona_conversation(
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
    expected = build_system_prompt(
        profile.model_copy(update={"allowed_tools": frozenset()}), brief=brief
    )
    assert created["persona"] == f"{expected}\n\n{PERCEPTION_GUIDANCE}"
    assert "recall_meeting_context" not in created["persona"]
    assert read_meeting_context(meeting_context_file(settings)) == expected_context(brief, profile)


@pytest.mark.parametrize("reformatted", [False, True])
async def test_join_brief_resume_reuses_the_matching_context_unchanged(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reformatted: bool,
) -> None:
    brief_path = write_brief(tmp_path)
    await join_persona_conversation(
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

    _created, audio, _outcome = await join_persona_conversation(
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
    "case",
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
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, str, ErrorCode],
) -> None:
    change, match, code = case
    await join_persona_conversation(
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
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief_path = write_brief(tmp_path)
    edited = [{"id": "demo-date", "text": "EDITED-FACT-MARKER"}]
    model = ToolCallingFakeModel(responses=[AIMessage(content="Hello.")])

    await join_persona_conversation(
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
    assert model.seen[0][0].content == (
        f"{build_system_prompt(profile, brief=original)}\n\n{PERCEPTION_GUIDANCE}"
    )
    assert "EDITED-FACT-MARKER" not in model.seen[0][0].content
    assert read_meeting_context(meeting_context_file(settings)) == expected_context(
        original, profile
    )
    forbid_resources(monkeypatch)
    with pytest.raises(FakeYouError, match="meeting context differs") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                settings, persona_path=write_persona(tmp_path), brief_path=brief_path
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_CONTEXT_DIFFERS


async def test_join_brief_snapshot_write_failure_starts_nothing(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    prepare_brief_join(settings, monkeypatch)

    def reject_replace(source: Any, target: Any) -> NoReturn:
        del source
        del target
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", reject_replace)

    with pytest.raises(FakeYouError, match="meeting context not written") as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path),
            ),
        )
    assert caught.value.code is ErrorCode.MEETING_CONTEXT_NOT_WRITTEN
    assert meeting_files(settings) == {}
    assert not settings.paths.journal_dir.exists()


async def test_join_journals_brief_metadata_without_brief_text(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await join_persona_conversation(
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


def test_v1_brief_without_roster_hint_still_loads(tmp_path: Path) -> None:
    payload = {
        "schema_version": "meeting_brief/v1",
        "owner_id": "o",
        "twin_id": "t",
        "meeting_id": "m",
        "agenda": ["Standup"],
    }
    path = tmp_path / "brief.json"
    path.write_text(json.dumps(payload))
    brief = load_meeting_brief(path, owner_id="o", twin_id="t", meeting_id="m")
    assert brief.roster_hint == ()
