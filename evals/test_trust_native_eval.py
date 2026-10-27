import asyncio
import json
from pathlib import Path
from typing import Any, override

import pytest
from langchain_core.messages import AIMessage

from evals._shared import ReplayOptions, suspended_gc
from evals._trust import (
    ROOT,
    EvalDelayedModel,
    build_case_memory,
    contract_model,
    load_trust_fixture,
    replay_trust,
)
from fakeyou.config.settings import Settings
from tests._conversation_fakes import ToolCallingFakeModel


class EvalFailingModel(ToolCallingFakeModel):
    @override
    async def _agenerate(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("scripted primary unavailable")


async def test_trust_native_fallback_and_sentence_cap_preserve_raw_reply() -> None:
    fixture = load_trust_fixture()
    case = {**fixture["cases"][0], "needs_recall": False}
    fallback = ToolCallingFakeModel(responses=[AIMessage(content="First. Second. Third.")])
    score = await replay_trust(
        lambda _: EvalFailingModel(responses=[AIMessage(content="unreachable")]),
        {**fixture, "cases": [case]},
        1,
        options=ReplayOptions(fallback=fallback),
    )
    turn = score.turns[0]
    assert turn["exception"] is None
    assert turn["reply"] == "First. Second. Third."
    assert turn["spoken"] == "First. Second."
    assert turn["model_calls"] == 2


async def test_trust_response_deadline_cancels_native_responder() -> None:
    fixture = load_trust_fixture()
    case = {**fixture["cases"][0], "needs_recall": False}
    settings = Settings()
    settings = settings.model_copy(
        update={
            "conversation": settings.conversation.model_copy(
                update={"response_timeout_seconds": 0.5}
            )
        }
    )
    model = EvalDelayedModel(responses=[AIMessage(content="Too late.")])
    with suspended_gc():
        score = await replay_trust(
            lambda _: model,
            {**fixture, "cases": [case]},
            1,
            options=ReplayOptions(settings=settings),
        )
    assert score.turns[0]["exception"].startswith("TimeoutError:")
    assert score.turns[0]["reply"] == ""
    assert score.turns[0]["latency_seconds"] < 4.0
    assert model.cancelled


async def test_trust_native_recall_failure_becomes_unavailable_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = load_trust_fixture()
    case = fixture["cases"][0]
    original = build_case_memory

    def failing_memory(case: dict[str, Any], group: str) -> Any:
        memory, stored, states = original(case, group)
        memory.search.side_effect = RuntimeError("scripted memory unavailable")
        return memory, stored, states

    monkeypatch.setitem(replay_trust.__globals__, "build_case_memory", failing_memory)
    score = await replay_trust(
        contract_model,
        {**fixture, "cases": [case]},
        1,
        options=ReplayOptions(settings=Settings()),
    )
    turn = score.turns[0]
    assert turn["exception"] is None
    assert turn["recall_called"] is True
    assert turn["memory_prefetch"][-1]["outcome"] == "unavailable"
    assert turn["tool_trajectory"] == []
    assert turn["model_calls"] == 1


async def test_trust_recall_deadline_records_cancelled_attempt_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = load_trust_fixture()
    original = build_case_memory
    memories: list[Any] = []

    def blocked_memory(case: dict[str, Any], group: str) -> Any:
        memory, stored, states = original(case, group)
        memory.search_gate = asyncio.Event()
        memories.append(memory)
        return memory, stored, states

    monkeypatch.setitem(replay_trust.__globals__, "build_case_memory", blocked_memory)
    settings = Settings()
    settings = settings.model_copy(
        update={
            "memory": settings.memory.model_copy(
                update={
                    "recall": settings.memory.recall.model_copy(update={"timeout_seconds": 0.5})
                }
            )
        }
    )
    score = await replay_trust(
        contract_model,
        {**fixture, "cases": fixture["cases"][:1]},
        1,
        options=ReplayOptions(settings=settings),
    )
    turn = score.turns[0]
    assert turn["exception"] is None
    assert turn["recall_called"] is True
    assert turn["memory_prefetch"][-1]["outcome"] == "unavailable"
    assert memories[0].search_cancelled.is_set()


async def test_failed_recall_after_success_records_current_prefetch_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = load_trust_fixture()
    case = fixture["cases"][0]
    case = {**case, "turns": [case["turns"][0], case["turns"][0]]}
    original = build_case_memory

    def second_search_fails(case: dict[str, Any], group: str) -> Any:
        memory, stored, states = original(case, group)
        search = memory.search.side_effect
        attempted = False

        async def search_once(*args: Any, **kwargs: Any) -> Any:
            nonlocal attempted
            if kwargs.get("search_filter") is not None:
                if attempted:
                    raise RuntimeError("second recall failed")
                attempted = True
            return await search(*args, **kwargs)

        memory.search.side_effect = search_once
        return memory, stored, states

    monkeypatch.setitem(replay_trust.__globals__, "build_case_memory", second_search_fails)
    score = await replay_trust(
        contract_model,
        {**fixture, "cases": [case]},
        1,
        options=ReplayOptions(settings=Settings()),
    )
    assert score.turns[0]["memory_prefetch"][-1]["outcome"] == "found"
    assert score.turns[1]["memory_prefetch"][-1]["outcome"] == "unavailable"


async def test_trust_profile_disallows_recall_in_native_prompt_and_prefetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = load_trust_fixture()
    profile = json.loads((ROOT / fixture["persona"]).read_text())
    profile["allowed_tools"] = []
    (tmp_path / "persona.json").write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setitem(replay_trust.__globals__, "ROOT", tmp_path)
    case = {**fixture["cases"][0], "needs_recall": False}
    score = await replay_trust(
        contract_model, {**fixture, "persona": "persona.json", "cases": [case]}, 1
    )
    assert score.turns[0]["memory_prefetch"] == []
    assert score.turns[0]["tool_trajectory"] == []
    assert score.prompts[case["id"]]["memory_prefetch"] is False
    assert "recall_meeting_context" not in score.prompts[case["id"]]["system_prompt"]


async def test_trust_runtime_applies_configured_spoken_cap_and_language() -> None:
    fixture = load_trust_fixture()
    case = {
        **fixture["cases"][0],
        "needs_recall": False,
        "turns": [{"say": "What is the update?", "contract_reply": "First. Second."}],
    }
    settings = Settings()
    settings = settings.model_copy(
        update={
            "conversation": settings.conversation.model_copy(update={"max_spoken_sentences": 1}),
            "speech": settings.speech.model_copy(update={"language": "en"}),
        }
    )
    score = await replay_trust(
        contract_model, {**fixture, "cases": [case]}, 1, options=ReplayOptions(settings=settings)
    )
    assert score.turns[0]["reply"] == "First. Second."
    assert score.turns[0]["spoken"] == "First."
    assert score.runtime["speech_language"] == "en"
    assert score.runtime["max_spoken_sentences"] == 1
