import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import SecretStr

from evals._shared import ArtifactMeta, ReplayOptions, sanitized_role
from evals._trust import (
    FIXTURE,
    contract_model,
    load_trust_fixture,
    passing_trust_reviews,
    replay_trust,
    summarize_trust,
    write_trust_artifacts,
)
from evals.runtime_evidence import runtime_metadata
from fakeyou.config.settings import CompatibleReply, OpenAIReply, Settings
from tests._sanitize_support import assert_no_synthetic_markers, marked_role


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
async def test_trust_role_setting_change_invalidates_writer_provenance(
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
    fixture = load_trust_fixture()
    fixture = {**fixture, "cases": fixture["cases"][:1]}
    score = await replay_trust(contract_model, fixture, 1, options=ReplayOptions(settings=start))
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
    write_trust_artifacts(
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
    reviews = passing_trust_reviews({"c01"}, 1)
    summary = summarize_trust(
        reviews, expected_case_ids={"c01"}, runs=1, score=score, settings=writer
    )
    assert summary["provenance_valid"] is manifest["provenance_valid"]
    assert summary["gate_passed"] is manifest["provenance_valid"]
    assert not summarize_trust(reviews, expected_case_ids={"c01"}, runs=1, score=score)[
        "gate_passed"
    ]


def test_runtime_role_snapshots_are_detached_and_sanitize_nested_credentials() -> None:
    role = CompatibleReply(
        base_url="https://user:START_URL_SECRET@synthetic.example/v1?token=START_URL_SECRET",
        model="snapshot-model",
        api_key=SecretStr("START_API_SECRET"),
        extra_body={
            "nested": {"api_key": "START_BODY_SECRET", "choices": ["start"]},
            "callback": "https://user:START_LINK_SECRET@synthetic.example/callback?key=START_LINK_SECRET",
        },
    )
    settings = Settings().model_copy(update={"reply": role, "reply_fallback": role})
    snapshot = runtime_metadata(settings)
    encoded = json.dumps(snapshot, sort_keys=True)
    for name in ("reply", "reply_fallback"):
        assert snapshot[name]["base_url"] == "https://synthetic.example/v1"
        assert snapshot[name]["api_key"] == "**********"
        assert snapshot[name]["extra_body"]["nested"]["api_key"] == "**********"
        assert snapshot[name]["extra_body"]["callback"] == "https://synthetic.example/callback"
    assert not any(
        marker in encoded
        for marker in (
            "START_URL_SECRET",
            "START_API_SECRET",
            "START_BODY_SECRET",
            "START_LINK_SECRET",
        )
    )
    assert role.extra_body is not None
    role.extra_body["nested"]["choices"].append("changed")
    role.extra_body["nested"]["api_key"] = "CHANGED_BODY_SECRET"
    assert json.dumps(snapshot, sort_keys=True) == encoded
    assert snapshot != runtime_metadata(settings)


def test_fallback_presence_is_part_of_runtime_provenance() -> None:
    settings = Settings().model_copy(update={"reply_fallback": None})
    missing = runtime_metadata(settings)
    configured = runtime_metadata(settings.model_copy(update={"reply_fallback": settings.reply}))
    assert missing["reply_fallback"] is None
    assert configured["reply_fallback"]["model"] == settings.reply.model
    assert missing != configured


def test_candidate_digest_includes_untracked_a_and_excludes_b(tmp_path: Path) -> None:
    from evals.runtime_evidence import capture_candidate, finish_candidate

    for name in (
        "src/fakeyou/persistence/brief.py",
        "src/fakeyou/orchestration/assembly.py",
        "src/fakeyou/debrief/debrief_schema.py",
        "evals/test_brief_replay_eval.py",
        "evals/fixtures/brief-scenarios.json",
    ):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("initial", encoding="utf-8")
    fixture_path = tmp_path / "evals/fixtures/brief-scenarios.json"
    first = capture_candidate(fixture_path, root=tmp_path)
    assert "src/fakeyou/orchestration/assembly.py" in first["start_files"]
    assert "src/fakeyou/debrief/debrief_schema.py" not in first["start_files"]
    (tmp_path / "src/fakeyou/debrief/debrief_schema.py").write_text("ongoing B", encoding="utf-8")
    assert finish_candidate(first, root=tmp_path)["unchanged"] is True
    (tmp_path / "src/fakeyou/persistence/brief.py").write_text("revised A", encoding="utf-8")
    finished = finish_candidate(first, root=tmp_path)
    assert finished["unchanged"] is False
    assert finished["start_sha256"] != finished["end_sha256"]
    assert "src/fakeyou/persistence/brief.py" in finished["changed_files"]


async def test_candidate_change_preserves_raw_turns_but_invalidates_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from evals import runtime_evidence

    tracked = tmp_path / "src/fakeyou/orchestration/assembly.py"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("start", encoding="utf-8")
    fixture_path = tmp_path / "evals/fixtures/trust-scenarios.json"
    fixture_path.parent.mkdir(parents=True)
    fixture = load_trust_fixture()
    fixture = {**fixture, "cases": fixture["cases"][:1]}
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    monkeypatch.setitem(
        replay_trust.__globals__,
        "capture_candidate",
        lambda path: runtime_evidence.capture_candidate(path, root=tmp_path),
    )
    monkeypatch.setitem(
        replay_trust.__globals__,
        "finish_candidate",
        lambda value: runtime_evidence.finish_candidate(value, root=tmp_path),
    )

    def changing_model(case: dict[str, Any]) -> BaseChatModel:
        tracked.write_text("end", encoding="utf-8")
        return contract_model(case)

    score = await replay_trust(
        changing_model, fixture, 1, options=ReplayOptions(fixture_path=fixture_path)
    )
    assert len(score.turns) == 1
    assert score.candidate["unchanged"] is False
    summary = summarize_trust(
        passing_trust_reviews({"c01"}, 1), expected_case_ids={"c01"}, runs=1, score=score
    )
    assert summary["gate_passed"] is False
    write_trust_artifacts(
        tmp_path / "artifacts",
        score=score,
        meta=ArtifactMeta(
            settings=Settings(),
            fixture=fixture,
            fixture_path=fixture_path,
            source_commit="end-commit",
            mode="contract",
            model_name="scripted",
        ),
    )
    manifest = json.loads((tmp_path / "artifacts/manifest.json").read_text())
    assert manifest["candidate"]["unchanged"] is False
    assert manifest["provenance_valid"] is False
    assert manifest["source_commit"] == score.candidate["source_commit"]
    assert len(json.loads((tmp_path / "artifacts/turns.json").read_text())) == 1


def test_sanitized_role_drops_unlisted_fields_and_masks_unenumerated_names() -> None:
    cleaned = sanitized_role(marked_role())

    assert_no_synthetic_markers(cleaned)
    assert "unlisted_field" not in cleaned
    assert cleaned["model"] == "marked-model"
    assert cleaned["base_url"] == "https://synthetic.example/v1"
    assert cleaned["extra_body"]["routing"] == {"choices": ["start"]}
