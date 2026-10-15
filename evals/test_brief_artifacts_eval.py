import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from evals._brief import (
    AID_CHECKS,
    FIXTURE,
    BriefScore,
    load_brief_fixture,
    passing_brief_reviews,
    replay_briefs,
    summarize_briefs,
    write_brief_artifacts,
)
from evals._shared import ArtifactMeta, ReplayOptions
from evals._trust import contract_model
from fakeyou.config.settings import CompatibleReply, OpenAIReply, Settings


def test_brief_artifacts_sanitize_credentials(tmp_path: Path) -> None:
    marker_url = "MARKER_URL_SECRET"
    marker_body = "MARKER_BODY_SECRET"
    settings = Settings()
    marked = CompatibleReply(
        base_url=f"https://user:{marker_url}@example.com:8000/v1?token={marker_url}",
        model="marked-model",
        api_key=SecretStr("marked-key"),
        extra_body={"api_key": marker_body, "safe": "keep"},
    )
    marked_settings = settings.model_copy(update={"reply": marked, "reply_fallback": marked})
    score = BriefScore()
    score.latencies.append(0.5)
    score.turns.append(
        {
            "case_id": "b01",
            "run_index": 0,
            "turn_index": 0,
            "rated": True,
            "model_calls": 1,
            "tool_trajectory": [],
            "aids": dict.fromkeys(AID_CHECKS, True),
        }
    )
    write_brief_artifacts(
        tmp_path,
        score=score,
        meta=ArtifactMeta(
            settings=marked_settings,
            fixture=load_brief_fixture(),
            fixture_path=FIXTURE,
            source_commit="test-commit",
            mode="contract",
            model_name="contract-test",
        ),
    )
    text = (tmp_path / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(text)
    assert "MARKER_URL_SECRET" not in text
    assert "MARKER_BODY_SECRET" not in text
    assert "marked-key" not in text
    for role in ("reply", "reply_fallback"):
        assert manifest[role]["base_url"] == "https://example.com:8000/v1"
        assert manifest[role]["extra_body"]["api_key"] == "**********"
        assert manifest[role]["extra_body"]["safe"] == "keep"


@pytest.mark.parametrize("role_name", ["reply", "reply_fallback"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unchanged", None),
        ("model", "different-writer-model"),
        ("provider", "openai"),
        ("timeout_seconds", 0.2),
        ("max_retries", 1),
        ("base_url", "https://different-writer.example/v1"),
        ("extra_body", {"routing": {"choices": ["changed"]}}),
    ],
)
async def test_brief_role_setting_change_invalidates_writer_provenance(
    tmp_path: Path,
    role_name: str,
    field: str,
    value: Any,
) -> None:
    initial_role = CompatibleReply(
        base_url="https://synthetic.example/v1",
        model="start-model",
        timeout_seconds=0.1,
        max_retries=0,
        extra_body={"routing": {"choices": ["start"]}},
    )
    start = Settings().model_copy(update={"reply": initial_role, "reply_fallback": initial_role})
    fixture = load_brief_fixture()
    fixture = {**fixture, "cases": fixture["cases"][:1]}
    score = await replay_briefs(contract_model, fixture, 1, options=ReplayOptions(settings=start))
    changed_role: CompatibleReply | OpenAIReply
    if field == "unchanged":
        changed_role = initial_role
    elif field == "provider":
        changed_role = OpenAIReply.model_validate(
            initial_role.model_dump(exclude={"provider", "extra_body"})
        )
    else:
        changed_role = initial_role.model_copy(update={field: value})
    writer = start.model_copy(update={role_name: changed_role})
    write_brief_artifacts(
        tmp_path,
        score=score,
        meta=ArtifactMeta(
            settings=writer,
            fixture=fixture,
            fixture_path=FIXTURE,
            source_commit=score.candidate["source_commit"],
            mode="contract",
            model_name="ToolCallingFakeModel(contract-scripted)",
        ),
    )
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["candidate"]["unchanged"] is True
    assert manifest["provenance_valid"] is (field == "unchanged")
    assert manifest["runtime"][role_name]["model"] == "start-model"
    reviews = passing_brief_reviews({"b01"}, 1)
    summary = summarize_briefs(
        reviews, expected_case_ids={"b01"}, runs=1, score=score, settings=writer
    )
    assert summary["provenance_valid"] is manifest["provenance_valid"]
    assert summary["gate_passed"] is manifest["provenance_valid"]
    assert not summarize_briefs(reviews, expected_case_ids={"b01"}, runs=1, score=score)[
        "gate_passed"
    ]


def test_brief_artifacts_refuse_to_overwrite_existing_raw_evidence(tmp_path: Path) -> None:
    target = tmp_path / "prompts.json"
    target.write_text("historical prompt", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_brief_artifacts(
            tmp_path,
            score=BriefScore(),
            meta=ArtifactMeta(
                settings=Settings(),
                fixture=load_brief_fixture(),
                fixture_path=FIXTURE,
                source_commit="candidate",
                mode="contract",
                model_name="scripted",
            ),
        )
    assert target.read_text(encoding="utf-8") == "historical prompt"
