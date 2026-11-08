import pytest

from fakeyou.orchestration.assembly import build_source_registry
from fakeyou.persistence.owner_model import FrozenOwnerModel, FrozenOwnerModelItem
from tests._cli_support import meeting_brief

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
def test_owner_knowledge_reaches_the_registry_only_outside_brief_only_meetings(
    scope: str, *, known: bool
) -> None:
    registry = build_source_registry(meeting_brief(knowledge_scope=scope), OWNER_MODEL)

    sources = {source for source, _ref, _text in registry.items()}
    assert sources - {"brief"} == ({"owner_model"} if known else set())
    assert list(registry.owner_model) == (["om-release"] if known else [])
