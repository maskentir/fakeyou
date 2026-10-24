from collections.abc import Sequence
from dataclasses import dataclass

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field

from fakeyou.config.identifiers import SessionId
from fakeyou.memory.persona import PersonaProfile
from fakeyou.persistence.brief import BriefItem, BriefPermission, MeetingBrief

BRIEF_PERMISSIONS: frozenset[BriefPermission] = frozenset(
    {"explain_facts", "discuss_options", "ask_brief_questions"}
)

_BRIEF_RULES = (
    "You prepare the brief for a meeting twin. The twin speaks for its owner and must know why "
    "the meeting happens, what it should settle, and which questions are still open. Use only "
    "the supplied owner notes, open questions and owner profile; never invent facts, owners, "
    "dates, decisions or approvals. Write every line in the language of the owner profile. "
    "The agenda lists what the meeting covers. Objectives are what the meeting should settle, "
    "one line each. Questions are what the twin should ask or find out, one line each. Leave a "
    "list empty when the sources do not support it."
)


class BriefDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agenda: tuple[str, ...] = Field(
        default=(), description="What the meeting covers, one line each."
    )
    objectives: tuple[str, ...] = Field(
        default=(), description="What the meeting should settle, one line each."
    )
    questions: tuple[str, ...] = Field(
        default=(), description="What the twin should ask or find out, one line each."
    )


@dataclass(frozen=True, slots=True)
class BriefSources:
    notes: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    previous_meeting: str | None = None


def _prompt(profile: PersonaProfile, meeting_id: SessionId, sources: BriefSources) -> str:
    lines = [
        f"Meeting: {meeting_id}",
        f"Owner profile: {profile.model_dump_json(indent=2)}",
    ]
    if sources.notes:
        lines.append("Owner notes:")
        lines.extend(f"- {note}" for note in sources.notes)
    if sources.open_questions:
        lines.append("Questions still open from the last meeting:")
        lines.extend(f"- {question}" for question in sources.open_questions)
    if sources.previous_meeting:
        lines.append(f"Last meeting: {sources.previous_meeting}")
    return "\n".join(lines)


def _items(prefix: str, texts: Sequence[str]) -> tuple[BriefItem, ...]:
    return tuple(
        BriefItem(id=f"{prefix}{index}", text=text) for index, text in enumerate(texts, start=1)
    )


async def draft_brief(
    model: BaseChatModel,
    profile: PersonaProfile,
    meeting_id: SessionId,
    sources: BriefSources,
) -> BriefDraft:
    agent = create_agent(
        model=model,
        tools=[],
        system_prompt=_BRIEF_RULES,
        response_format=ToolStrategy(BriefDraft, handle_errors=False),
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": _prompt(profile, meeting_id, sources)}]}
    )
    draft = result.get("structured_response")
    if not isinstance(draft, BriefDraft):
        raise TypeError("the brief draft is missing")
    return draft


def build_brief(
    draft: BriefDraft,
    profile: PersonaProfile,
    meeting_id: SessionId,
) -> MeetingBrief:
    return MeetingBrief(
        schema_version="meeting_brief/v2",
        owner_id=profile.owner_id,
        twin_id=profile.twin_id,
        meeting_id=meeting_id,
        agenda=tuple(draft.agenda),
        objectives=_items("o", draft.objectives),
        questions=_items("q", draft.questions),
        permissions=frozenset(BRIEF_PERMISSIONS),
        knowledge_scope="persona_memory",
    )
