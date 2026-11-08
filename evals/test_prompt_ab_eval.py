from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from evals._prompt_ab import PromptVariant, compare_variants, prompt_digest, replay_variants
from evals._shared import cited_reply
from fakeyou.memory.persona import PersonaProfile
from fakeyou.orchestration.persona_prompt import build_persona_prompt
from fakeyou.persistence.owner_model import FrozenOwnerModel
from tests._conversation_fakes import ToolCallingFakeModel

FIXTURE = {
    "persona": "examples/persona.example.json",
    "owner_model": {
        "version": 1,
        "items": [
            {
                "item_id": "beta",
                "kind": "fact",
                "topic": "imported fact",
                "statement": "The Orchid beta launch is planned for 14 October 2026",
                "confidence": "firm",
                "disclosure": "room_speakable",
            }
        ],
    },
    "sessions": [
        {
            "meeting_id": "m1",
            "memory": [],
            "turns": [{"say": "When is the beta launch?", "must_mention": ["14 october"]}],
        }
    ],
}


def _xml_variant(profile: PersonaProfile, *, owner_model: FrozenOwnerModel | None) -> str:
    return "<role>marked-xml-variant</role>\n\n" + build_persona_prompt(
        profile, owner_model=owner_model
    )


def _system_texts(model: ToolCallingFakeModel) -> list[str]:
    return [
        str(message.content)
        for batch in model.seen
        for message in batch
        if isinstance(message, SystemMessage)
    ]


async def test_variants_reach_the_model_and_score_separately() -> None:
    created: list[ToolCallingFakeModel] = []

    def factory() -> ToolCallingFakeModel:
        replies: list[BaseMessage] = [
            AIMessage(content=cited_reply("The beta launch is on 14 October 2026.", "om-beta"))
        ]
        model = ToolCallingFakeModel(responses=replies)
        created.append(model)
        return model

    variants = (
        PromptVariant(name="baseline", build=build_persona_prompt),
        PromptVariant(name="xml", build=_xml_variant),
    )
    scores = await replay_variants(factory, FIXTURE, 1, variants)

    assert set(scores) == {"baseline", "xml"}
    assert all(score.rate("must_mention") == 1.0 for score in scores.values())
    texts = [_system_texts(model)[0] for model in created]
    assert "marked-xml-variant" not in texts[0]
    assert "marked-xml-variant" in texts[1]
    assert prompt_digest(variants[0], FIXTURE) != prompt_digest(variants[1], FIXTURE)
    comparison = compare_variants(scores)
    assert comparison["baseline"]["passed"] is True
    assert comparison["xml"]["passed"] is True
