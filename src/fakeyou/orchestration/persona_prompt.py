from dataclasses import dataclass

from fakeyou.config.settings import DEFAULTS, RecallStrategy
from fakeyou.conversation.agent import (
    ENDING_RULE,
    MEETING_GUIDANCE,
    SPOKEN_LANGUAGE_GUIDANCE,
)
from fakeyou.conversation.prompt_builder import PromptSections
from fakeyou.memory.persona import (
    BRIEF_ONLY_PROFILE_FIELDS,
    BRIEF_PROMPT_FIELDS,
    BRIEF_RULES,
    GOALS_RULE,
    IDENTITY_RULE,
    KNOWN_FACT_RULES,
    NATURAL_SPEECH_RULE,
    PROMPT_PROFILE_FIELDS,
    PROVENANCE_RULE,
    RECALL_RULES,
    SOURCE_RULES,
    PersonaProfile,
)
from fakeyou.perception.context import PERCEPTION_GUIDANCE
from fakeyou.persistence.brief import MeetingBrief

PARAGRAPH = "\n\n"


@dataclass(frozen=True, slots=True)
class _PromptParts:
    role: tuple[str, ...]
    authority: str = ""
    goals: str = ""
    recall: str = ""
    profile_json: str = ""
    brief_rules: str = ""
    brief_json: str = ""
    provenance: str = ""
    natural_speech: str = ""
    perception: str = ""


def _paragraphs(*parts: str) -> str:
    return PARAGRAPH.join(part for part in parts if part)


def _prompt_parts(
    profile: PersonaProfile | None,
    *,
    brief: MeetingBrief | None,
    recall_strategy: RecallStrategy,
    language: str,
    perception: bool,
) -> _PromptParts:
    perception_guidance = PERCEPTION_GUIDANCE if perception else ""
    if profile is None:
        return _PromptParts(
            role=(f"{MEETING_GUIDANCE} {SPOKEN_LANGUAGE_GUIDANCE[language]}",),
            perception=perception_guidance,
        )
    scope = brief.knowledge_scope if brief is not None else None
    persona_knowledge = scope != "brief_only"
    recall = persona_knowledge and "recall_meeting_context" in profile.allowed_tools
    profile_fields = PROMPT_PROFILE_FIELDS if persona_knowledge else BRIEF_ONLY_PROFILE_FIELDS
    return _PromptParts(
        role=(MEETING_GUIDANCE, SPOKEN_LANGUAGE_GUIDANCE[profile.language]),
        authority=f"{IDENTITY_RULE} {KNOWN_FACT_RULES[scope]} {SOURCE_RULES}",
        goals=GOALS_RULE if persona_knowledge else "",
        recall=RECALL_RULES[recall_strategy] if recall else "",
        profile_json=profile.model_dump_json(include=profile_fields, indent=2),
        brief_rules=BRIEF_RULES if brief is not None else "",
        brief_json=(
            brief.model_dump_json(include=BRIEF_PROMPT_FIELDS, indent=2)
            if brief is not None
            else ""
        ),
        provenance=PROVENANCE_RULE if recall else "",
        natural_speech=NATURAL_SPEECH_RULE,
        perception=perception_guidance,
    )


def persona_prompt_sections(
    profile: PersonaProfile | None,
    *,
    brief: MeetingBrief | None = None,
    recall_strategy: RecallStrategy = "tool",
    language: str = DEFAULTS.speech.language,
    perception: bool = False,
) -> PromptSections:
    parts = _prompt_parts(
        profile,
        brief=brief,
        recall_strategy=recall_strategy,
        language=language,
        perception=perception,
    )
    return PromptSections(
        role=_paragraphs(*parts.role),
        authority=_paragraphs(parts.authority, parts.goals, parts.brief_rules),
        persona=parts.profile_json,
        brief=parts.brief_json,
        memory_rules=_paragraphs(parts.recall, parts.provenance),
        tools=parts.perception,
        output_rules=_paragraphs(parts.natural_speech, ENDING_RULE),
    )


def build_persona_prompt(
    profile: PersonaProfile | None,
    *,
    brief: MeetingBrief | None = None,
    recall_strategy: RecallStrategy = "tool",
    language: str = DEFAULTS.speech.language,
    perception: bool = False,
) -> str:
    parts = _prompt_parts(
        profile,
        brief=brief,
        recall_strategy=recall_strategy,
        language=language,
        perception=perception,
    )
    return _paragraphs(
        *parts.role,
        parts.authority,
        parts.goals,
        parts.recall,
        f"Owner profile JSON:\n{parts.profile_json}" if parts.profile_json else "",
        parts.brief_rules,
        f"Meeting brief JSON:\n{parts.brief_json}" if parts.brief_json else "",
        parts.provenance,
        parts.natural_speech,
        ENDING_RULE,
        parts.perception,
    )
