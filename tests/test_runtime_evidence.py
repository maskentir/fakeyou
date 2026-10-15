from __future__ import annotations

from pathlib import Path

from evals.runtime_evidence import (
    ModelCallCounter,
    ResponderContext,
    candidate_files,
    debrief_files,
    native_responder,
)
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.conversation import LangGraphResponder
from fakeyou.memory.persona import PersonaProfile
from tests._conversation_fakes import ToolCallingFakeModel
from tests._memory_fakes import make_graphiti


async def test_native_responder_works_without_database_url() -> None:
    settings = Settings()
    settings = settings.model_copy(
        update={"database": settings.database.model_copy(update={"url": None})}
    )
    profile = PersonaProfile(
        owner_id=SessionId("owner"),
        twin_id=SessionId("twin"),
        display_name="Twin",
        role="assistant",
    )
    async with native_responder(
        model=ToolCallingFakeModel(responses=[]),
        context=ResponderContext(
            settings=settings,
            profile=profile,
            meeting_id="m1",
            memory=make_graphiti(),
            brief=None,
            fallback=None,
            counter=ModelCallCounter(),
            trajectory=[],
        ),
    ) as responder:
        assert isinstance(responder, LangGraphResponder)


def test_candidate_files_name_existing_runtime_sources() -> None:
    root = Path(__file__).resolve().parent.parent
    names = candidate_files(root)

    assert [name for name in names if not (root / name).is_file()] == []
    assert "src/fakeyou/orchestration/runtime.py" in names
    assert "evals/_replay.py" in names
    assert not [name for name in names if "debrief" in name]


def test_candidate_scope_covers_js_assets_and_the_whole_debrief_package() -> None:
    root = Path(__file__).resolve().parent.parent

    assert "src/fakeyou/assets/stream_capture.js" in candidate_files(root)
    assert "src/fakeyou/debrief/__init__.py" in debrief_files(root)
    assert not [name for name in candidate_files(root) if name.startswith("src/fakeyou/debrief/")]


def test_file_added_during_the_run_marks_the_candidate_changed(tmp_path: Path) -> None:
    from evals.runtime_evidence import capture_candidate, finish_candidate

    package = tmp_path / "src" / "fakeyou"
    package.mkdir(parents=True)
    (package / "existing.py").write_text("x = 1\n", encoding="utf-8")
    fixture = tmp_path / "evals" / "fixtures" / "brief-scenarios.json"
    fixture.parent.mkdir(parents=True)
    fixture.write_text("{}", encoding="utf-8")
    candidate = capture_candidate(fixture, root=tmp_path)
    (package / "added.py").write_text("y = 2\n", encoding="utf-8")

    finished = finish_candidate(candidate, root=tmp_path)

    assert finished["unchanged"] is False
    assert "src/fakeyou/added.py" in finished["changed_files"]
