import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from evals._replay import (
    MAX_TRAILING_QUESTION_RATE,
    REPLY_LATENCY_BUDGET_SECONDS,
    REQUIRED_CHECKS,
    REQUIRED_PASS_RATE,
    ROOT,
    ReplayScore,
    replay,
)
from fakeyou.memory.persona import PersonaProfile


@dataclass(frozen=True, slots=True)
class PromptVariant:
    name: str
    build: Callable[[PersonaProfile], str]


def prompt_digest(variant: PromptVariant, fixture: dict[str, Any]) -> str:
    profile = PersonaProfile.model_validate_json((ROOT / fixture["persona"]).read_bytes())
    return hashlib.sha256(variant.build(profile).encode("utf-8")).hexdigest()


async def replay_variants(
    model_factory: Callable[[], BaseChatModel],
    fixture: dict[str, Any],
    runs: int,
    variants: Sequence[PromptVariant],
    *,
    streaming: bool = False,
) -> dict[str, ReplayScore]:
    scores: dict[str, ReplayScore] = {}
    for variant in variants:
        scores[variant.name] = await replay(
            model_factory, fixture, runs, streaming=streaming, prompt_builder=variant.build
        )
    return scores


def compare_variants(scores: dict[str, ReplayScore]) -> dict[str, Any]:
    comparison: dict[str, Any] = {}
    for name, score in scores.items():
        summary = score.summary()
        passed = (
            all(score.rate(check) >= REQUIRED_PASS_RATE for check in REQUIRED_CHECKS)
            and 1 - score.rate("no_trailing_question") <= MAX_TRAILING_QUESTION_RATE
            and summary["latency_p95_s"] <= REPLY_LATENCY_BUDGET_SECONDS
        )
        comparison[name] = {"summary": summary, "passed": passed}
    return comparison
