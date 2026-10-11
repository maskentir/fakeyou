from __future__ import annotations

from evals.runtime_evidence import ModelCallCounter, native_responder
from fakeyou.config.settings import Settings
from fakeyou.conversation.conversation import LangGraphResponder
from fakeyou.memory.persona import PersonaProfile
from tests.fakes import ToolCallingFakeModel, make_graphiti


async def test_native_responder_works_without_database_url() -> None:
    settings = Settings()
    settings = settings.model_copy(
        update={"database": settings.database.model_copy(update={"url": None})}
    )
    profile = PersonaProfile(
        owner_id="owner",
        twin_id="twin",
        display_name="Twin",
        role="assistant",
    )
    async with native_responder(
        model=ToolCallingFakeModel(responses=[]),
        settings=settings,
        profile=profile,
        meeting_id="m1",
        memory=make_graphiti(),
        brief=None,
        fallback=None,
        counter=ModelCallCounter(),
        trajectory=[],
    ) as responder:
        assert isinstance(responder, LangGraphResponder)
