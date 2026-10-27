import pytest

from evals._judge import JudgeConfig, grades_itself, judge_config
from fakeyou.config.settings import Settings


def test_no_judge_key_means_no_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EVAL_JUDGE_API_KEY", raising=False)
    monkeypatch.setenv("FAKEYOU_REPLY__API_KEY", "reply-key")

    assert judge_config() is None


def test_the_judge_is_configured_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("EVAL_JUDGE_PROVIDER", "openai")
    monkeypatch.setenv("EVAL_JUDGE_MODEL", "muse-spark-1.2")
    monkeypatch.setenv("EVAL_JUDGE_BASE_URL", "https://judge.example/v1")

    assert judge_config() == JudgeConfig(
        "openai", "muse-spark-1.2", "judge-key", "https://judge.example/v1"
    )


def test_an_unknown_provider_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVAL_JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("EVAL_JUDGE_PROVIDER", "muse")

    with pytest.raises(ValueError, match="EVAL_JUDGE_PROVIDER"):
        judge_config()


def test_a_judge_that_is_the_reply_model_grades_itself() -> None:
    settings = Settings()
    judge = JudgeConfig("gemini", settings.reply.model, "judge-key")

    assert grades_itself(judge, settings) is True
    assert (
        grades_itself(JudgeConfig("gemini", f"{settings.reply.model}-other", "k"), settings)
        is False
    )
