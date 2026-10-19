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


def test_participation_decider_defaults_are_conservative() -> None:
    decider = Settings().participation_decider
    assert decider.model == "gemini-3.8-flash"
    assert decider.provider == "gemini"
    assert decider.window_turns == 6
    assert decider.speak_threshold == 0.70
    assert decider.clarify_threshold == 0.50
    assert decider.enabled is True
