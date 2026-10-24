from langchain_core.messages import AIMessage

from fakeyou.config.identifiers import SessionId
from fakeyou.memory.persona import PersonaProfile
from fakeyou.orchestration.brief_preparation import (
    BriefDraft,
    BriefSources,
    build_brief,
    draft_brief,
)
from tests._conversation_fakes import ToolCallingFakeModel

PERSONA = {
    "owner_id": "hard_owner",
    "twin_id": "hard_twin",
    "display_name": "Rani",
    "role": "Test role",
    "language": "en",
    "goals": ["Keep the weekly sync on schedule"],
    "vocabulary": ["Orchid"],
}


def _profile() -> PersonaProfile:
    return PersonaProfile.model_validate(PERSONA)


def _draft_response() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "BriefDraft",
                "args": {
                    "agenda": ["Weekly sync"],
                    "objectives": ["Settle the beta date"],
                    "questions": ["Who updates the checklist?"],
                },
                "id": "draft",
            }
        ],
    )


async def test_the_brief_draft_becomes_a_brief_with_ids_and_safe_permissions() -> None:
    model = ToolCallingFakeModel(responses=[_draft_response()])

    draft = await draft_brief(
        model,
        _profile(),
        SessionId("meeting-1"),
        BriefSources(notes=("the owner wants the date settled",), open_questions=("question:q1",)),
    )
    brief = build_brief(draft, _profile(), SessionId("meeting-1"))

    assert brief.agenda == ("Weekly sync",)
    assert [item.id for item in brief.objectives] == ["o1"]
    assert [item.id for item in brief.questions] == ["q1"]
    assert brief.permissions == frozenset(
        {"explain_facts", "discuss_options", "ask_brief_questions"}
    )
    assert "meet_act" not in brief.permissions
    assert "meet_chat" not in brief.permissions
    assert brief.knowledge_scope == "persona_memory"
    assert brief.owner_id == "hard_owner"
    assert brief.meeting_id == "meeting-1"


async def test_the_draft_prompt_carries_the_owner_sources() -> None:
    model = ToolCallingFakeModel(responses=[_draft_response()])

    await draft_brief(
        model,
        _profile(),
        SessionId("meeting-1"),
        BriefSources(notes=("the owner wants the date settled",), open_questions=("question:q1",)),
    )

    prompt = model.seen[-1][-1].text
    assert "the owner wants the date settled" in prompt
    assert "question:q1" in prompt
    assert "Keep the weekly sync on schedule" in prompt


def test_an_empty_draft_yields_a_brief_without_claims() -> None:
    brief = build_brief(BriefDraft(), _profile(), SessionId("meeting-1"))

    assert brief.agenda == ()
    assert brief.objectives == ()
    assert brief.questions == ()
