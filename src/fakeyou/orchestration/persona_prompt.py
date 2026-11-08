import json
from dataclasses import dataclass

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.agent import (
    ENDING_RULE,
    MEETING_GUIDANCE,
    SPOKEN_LANGUAGE_GUIDANCE,
)
from fakeyou.memory.persona import (
    ATTENTION_RULE,
    BRIEF_ONLY_PROFILE_FIELDS,
    BRIEF_PROMPT_FIELDS,
    BRIEF_RULES,
    CONFIDENCE_RULE,
    CONFIDENTIALITY_RULE,
    DISCLOSURE_RULES,
    GOALS_RULE,
    IDENTITY_RULE,
    KNOWN_FACT_RULES,
    NATURAL_SPEECH_RULE,
    OWNER_MODEL_RULE,
    PROMPT_PROFILE_FIELDS,
    PROVENANCE_RULE,
    RECALL_RULE,
    SOURCE_RULES,
    TOOL_ECONOMY_RULE,
    PersonaProfile,
    profile_prompt_json,
)
from fakeyou.perception.context import PERCEPTION_GUIDANCE
from fakeyou.persistence.brief import MeetingBrief
from fakeyou.persistence.owner_model import FrozenOwnerModel, FrozenOwnerModelItem

PARAGRAPH = "\n\n"
MAX_OWNER_MODEL_ITEMS = 40


@dataclass(frozen=True, slots=True)
class _PromptParts:
    role: tuple[str, ...]
    authority: str = ""
    goals: str = ""
    recall: str = ""
    profile_json: str = ""
    brief_rules: str = ""
    brief_json: str = ""
    owner_model_json: str = ""
    provenance: str = ""
    natural_speech: str = ""
    confidence: str = ""
    confidentiality: str = ""
    perception: str = ""


def _paragraphs(*parts: str) -> str:
    return PARAGRAPH.join(part for part in parts if part)


def owner_model_prompt_json(owner_model: FrozenOwnerModel | None) -> str:
    if owner_model is None:
        return ""
    speakable = [item for item in owner_model.items if item.disclosure == "room_speakable"]
    if not speakable:
        return ""
    return json.dumps(
        [_owner_model_prompt_item(item) for item in speakable[-MAX_OWNER_MODEL_ITEMS:]],
        indent=2,
    )


def _owner_model_prompt_item(item: FrozenOwnerModelItem) -> dict[str, str]:
    rendered = {
        "id": item.ref,
        "kind": item.kind,
        "topic": item.topic,
        "statement": item.statement,
        "confidence": item.confidence,
    }
    if item.valid_at is not None:
        rendered["true_since"] = item.valid_at.date().isoformat()
    return rendered


def _prompt_parts(
    profile: PersonaProfile | None,
    *,
    brief: MeetingBrief | None,
    owner_model: FrozenOwnerModel | None,
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
    owner_model_json = owner_model_prompt_json(owner_model) if persona_knowledge else ""
    return _PromptParts(
        role=(MEETING_GUIDANCE, SPOKEN_LANGUAGE_GUIDANCE[profile.language]),
        authority=" ".join(
            rule
            for rule in (
                IDENTITY_RULE,
                DISCLOSURE_RULES[profile.ai_disclosure],
                CONFIDENTIALITY_RULE,
                ATTENTION_RULE,
                TOOL_ECONOMY_RULE,
                KNOWN_FACT_RULES[scope],
                OWNER_MODEL_RULE if owner_model_json else "",
                SOURCE_RULES,
            )
            if rule
        ),
        goals=GOALS_RULE if persona_knowledge else "",
        recall=RECALL_RULE if recall else "",
        profile_json=profile_prompt_json(profile, profile_fields, with_fact_ids=persona_knowledge),
        brief_rules=BRIEF_RULES if brief is not None else "",
        brief_json=(
            brief.model_dump_json(include=BRIEF_PROMPT_FIELDS, indent=2)
            if brief is not None
            else ""
        ),
        owner_model_json=owner_model_json,
        provenance=PROVENANCE_RULE if recall else "",
        natural_speech=NATURAL_SPEECH_RULE,
        confidence=CONFIDENCE_RULE,
        confidentiality=CONFIDENTIALITY_RULE,
        perception=perception_guidance,
    )


def build_persona_prompt(
    profile: PersonaProfile | None,
    *,
    brief: MeetingBrief | None = None,
    owner_model: FrozenOwnerModel | None = None,
    language: str = DEFAULTS.speech.language,
    perception: bool = False,
) -> str:
    parts = _prompt_parts(
        profile,
        brief=brief,
        owner_model=owner_model,
        language=language,
        perception=perception,
    )
    return _paragraphs(
        *parts.role,
        parts.authority,
        parts.goals,
        parts.recall,
        f"Owner profile JSON:\n{parts.profile_json}" if parts.profile_json else "",
        f"Owner model JSON:\n{parts.owner_model_json}" if parts.owner_model_json else "",
        parts.brief_rules,
        f"Meeting brief JSON:\n{parts.brief_json}" if parts.brief_json else "",
        parts.provenance,
        parts.natural_speech,
        parts.confidence,
        ENDING_RULE,
        parts.perception,
    )
