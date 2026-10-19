import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from evals._shared import ArtifactMeta
from evals._trust import (
    AID_CHECKS,
    FIXTURE,
    TrustScore,
    load_trust_fixture,
    write_trust_artifacts,
)
from fakeyou.config.settings import CompatibleReply, Settings


def test_trust_artifacts_sanitize_credentials(tmp_path: Path) -> None:
    marker_url = "MARKER_URL_SECRET"
    marker_body = "MARKER_BODY_SECRET"
    settings = Settings()
    marked = CompatibleReply(
        base_url=f"https://user:{marker_url}@example.com:8000/v1?token={marker_url}",
        model="marked-model",
        api_key=SecretStr("marked-key"),
        extra_body={"api_key": marker_body, "safe": "keep"},
    )
    marked_settings = settings.model_copy(update={"reply": marked})
    score = TrustScore()
    score.latencies.append(0.5)
    score.turns.append(
        {
            "case_id": "c01",
            "run_index": 0,
            "turn_index": 0,
            "rated": True,
            "aids": dict.fromkeys(AID_CHECKS, True),
        }
    )
    fixture = {"persona": "examples/persona.example.json", "cases": [{"id": "c01"}]}
    write_trust_artifacts(
        tmp_path,
        score=score,
        meta=ArtifactMeta(
            settings=marked_settings,
            fixture=fixture,
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
    assert manifest["reply"]["base_url"] == "https://example.com:8000/v1"
    assert manifest["reply"]["extra_body"]["api_key"] == "**********"
    assert manifest["reply"]["extra_body"]["safe"] == "keep"


def test_trust_artifacts_refuse_to_overwrite_existing_raw_evidence(tmp_path: Path) -> None:
    target = tmp_path / "turns.json"
    target.write_text("historical output", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_trust_artifacts(
            tmp_path,
            score=TrustScore(),
            meta=ArtifactMeta(
                settings=Settings(),
                fixture=load_trust_fixture(),
                fixture_path=FIXTURE,
                source_commit="candidate",
                mode="contract",
                model_name="scripted",
            ),
        )
    assert target.read_text(encoding="utf-8") == "historical output"
