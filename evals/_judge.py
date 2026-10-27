import os
from dataclasses import dataclass
from typing import Literal, cast, get_args

from deepeval.models import DeepEvalBaseLLM, GeminiModel, OpenAIModel

from fakeyou.config.settings import Settings

JudgeProvider = Literal["openai", "gemini"]
DEFAULT_JUDGE_MODELS: dict[JudgeProvider, str] = {
    "openai": "gpt-4.1-mini",
    "gemini": "gemini-3.8-flash",
}


@dataclass(frozen=True, slots=True)
class JudgeConfig:
    provider: JudgeProvider
    model: str
    api_key: str
    base_url: str | None = None

    def deepeval(self) -> DeepEvalBaseLLM:
        if self.provider == "gemini":
            return GeminiModel(model=self.model, api_key=self.api_key, temperature=0)
        return OpenAIModel(
            model=self.model, api_key=self.api_key, base_url=self.base_url, temperature=0
        )


def judge_config() -> JudgeConfig | None:
    api_key = os.environ.get("EVAL_JUDGE_API_KEY", "").strip()
    if not api_key:
        return None
    provider = os.environ.get("EVAL_JUDGE_PROVIDER", "openai").strip()
    if provider not in get_args(JudgeProvider):
        raise ValueError(f"EVAL_JUDGE_PROVIDER must be one of {get_args(JudgeProvider)}")
    chosen = cast(JudgeProvider, provider)
    return JudgeConfig(
        provider=chosen,
        model=os.environ.get("EVAL_JUDGE_MODEL", "").strip() or DEFAULT_JUDGE_MODELS[chosen],
        api_key=api_key,
        base_url=os.environ.get("EVAL_JUDGE_BASE_URL", "").strip() or None,
    )


def grades_itself(config: JudgeConfig, settings: Settings) -> bool:
    return config.model == settings.reply.model
