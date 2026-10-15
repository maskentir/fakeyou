import json
from pathlib import Path
from typing import Any

import pytest

from fakeyou.foundation.sentences import sentence_end

FIXTURE = Path(__file__).parent / "fixtures" / "sentence-boundaries.json"
CASES: list[dict[str, Any]] = json.loads(FIXTURE.read_text())


@pytest.mark.parametrize(
    "case", CASES, ids=lambda case: f"{case['language']}:{case['text'][:20]!r}"
)
def test_sentence_end_matches_the_pipecat_golden(case: dict[str, Any]) -> None:
    assert sentence_end(case["text"], language=case["language"]) == case["boundary"]
