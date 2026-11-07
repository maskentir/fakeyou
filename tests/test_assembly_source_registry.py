import pytest

from fakeyou.memory.persona import PersonaProfile
from fakeyou.orchestration.assembly import build_source_registry
from fakeyou.persistence.owner_model import FrozenOwnerModel, FrozenOwnerModelItem
from tests._cli_support import PERSONA_DATA, meeting_brief

OWNER_MODEL = FrozenOwnerModel(
    version=1,
    items=(
        FrozenOwnerModelItem(
            item_id="release",
            kind="position",
            topic="release",
            statement="Ship after the load test.",
            confidence="firm",
            disclosure="room_speakable",
        ),
    ),
)


@pytest.mark.parametrize(("scope", "known"), [("persona_memory", True), ("brief_only", False)])
def test_persona_knowledge_reaches_the_registry_only_outside_brief_only_meetings(
    scope: str, *, known: bool
) -> None:
    profile = PersonaProfile.model_validate(
        {**PERSONA_DATA, "verified_facts": ["Rani owns the launch."]}
    )
    brief = meeting_brief(knowledge_scope=scope)

    registry = build_source_registry(profile, brief, OWNER_MODEL)

    sources = {source for source, _ref, _text in registry.items()}
    assert ("profile" in sources, "owner_model" in sources) == (known, known)
    assert bool(registry.profile) is known
    assert bool(registry.owner_model) is known
