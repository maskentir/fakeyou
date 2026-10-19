from pathlib import Path

from pydantic import BaseModel

from fakeyou.memory.persona import PersonaProfile

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


EXAMPLE_BRIEF = EXAMPLES / "meeting-brief.example.json"


PERSONA_GOAL = "PERSONA_GOAL_MARKER keep the vendor sync under thirty minutes"


PERSONA_FACT = "PERSONA_FACT_MARKER the Orchid pilot budget is 40 million"


def profile_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "owner_id": "owner-a",
        "twin_id": "twin-a",
        "display_name": "Rani Twin",
        "role": "Product lead for the synthetic Orchid project",
        "language": "id",
        "speaking_style": "Brief and friendly",
        "goals": ["Keep the weekly sync on schedule"],
        "verified_facts": ["The Orchid beta launch is planned for 14 October 2026"],
        "allowed_tools": ["recall_meeting_context"],
    }
    data.update(overrides)
    return data


def profile(**overrides: object) -> PersonaProfile:
    return PersonaProfile.model_validate(profile_data(**overrides))


def revised[Model: BaseModel](model: Model, **updates: object) -> Model:
    return model.model_validate(model.model_dump() | updates)
