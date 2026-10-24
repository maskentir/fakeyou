import os
from pathlib import Path

import pytest

from fakeyou.config.settings import Settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in os.environ:
        if key.upper().startswith("FAKEYOU_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


def test_the_turn_judge_runs_on_the_reply_model_by_default() -> None:
    judge = Settings().turn_judge
    assert judge.model is None
    assert judge.window_lines == 8
    assert judge.reply_threshold == 0.60
    assert judge.clarify_threshold == 0.50
    assert judge.timeout_seconds == 4.0
