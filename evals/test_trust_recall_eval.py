import json
from typing import Any

from evals._shared import token_present
from evals._trust import (
    ROOT,
    build_case_memory,
    case_storage,
    load_trust_fixture,
)
from fakeyou.memory.memory import RECALL_EMPTY_RESULT, create_memory_tools
from fakeyou.memory.persona import PersonaProfile


async def recall_output_for(case: dict[str, Any], group_id: str) -> str:
    memory, stored, states = build_case_memory(case, group_id)
    with case_storage(memory, stored, states):
        _remember, production = create_memory_tools(memory, group_id=group_id)
        return str(await production.ainvoke({"query": str(case["turns"][0]["say"])}))


async def test_case_memory_match_any_returns_facts_without_lexical_overlap() -> None:
    stored = {
        "m1": {
            "group": "own",
            "label": "Google Meet m1 transcript",
            "source_at": "2026-09-23T03:10:00+00:00",
        }
    }
    turns = [{"say": "What is the budget status?", "contract_reply": "Bridge repaint starts June."}]
    matched = {
        "memory": {
            "facts": ["Bridge repaint starts June."],
            "episodes": {"Bridge repaint starts June.": ["m1"]},
            "stored": stored,
            "match": "any",
        },
        "turns": turns,
    }
    assert "Bridge repaint starts June." in await recall_output_for(matched, "team_alpha")
    sensitive = {
        "memory": {
            "facts": ["Bridge repaint starts June."],
            "episodes": {"Bridge repaint starts June.": ["m1"]},
            "stored": stored,
        },
        "turns": turns,
    }
    assert await recall_output_for(sensitive, "team_alpha") == RECALL_EMPTY_RESULT


async def test_contract_recall_uses_production_evidence_schema() -> None:
    fixture = load_trust_fixture()
    profile = PersonaProfile.model_validate_json((ROOT / fixture["persona"]).read_bytes())
    group_id = profile.memory_group
    by_id = {case["id"]: case for case in fixture["cases"]}
    merged = await recall_output_for(by_id["c01"], group_id)
    payload = json.loads(merged)
    assert payload["schema"] == "memory_evidence/v1"
    assert len(payload["facts"]) == 2
    kinds = {source["kind"] for fact in payload["facts"] for source in fact["sources"]}
    assert kinds == {"owner_note", "meeting_transcript"}
    assert payload["excluded"] == {"pending": 0, "superseded": 0, "missing_source": 0}
    for case_id in ("c07", "c08", "c14"):
        assert await recall_output_for(by_id[case_id], group_id) == RECALL_EMPTY_RESULT
    corrected = await recall_output_for(by_id["c10"], group_id)
    for token in by_id["c10"].get("allow", []):
        assert token_present(str(token), corrected)
    for token in by_id["c10"].get("forbid", []):
        assert not token_present(str(token), corrected)
    assert json.loads(corrected)["excluded"]["superseded"] == 1
