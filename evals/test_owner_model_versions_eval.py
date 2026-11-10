import json
import os
from pathlib import Path
from typing import Any, override

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import fakeyou.orchestration.assembly as assembly
from evals._conversation_replay import (
    Verdict,
    VersionDelta,
    VersionReplayReport,
    load_versioned_cases,
    model_judge,
    replay_owner_model_versions,
)
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.conversation.turn_judge import JudgeWindow, TurnJudgement
from fakeyou.orchestration.providers import create_chat_model, create_debrief_chat_model
from tests._conversation_fakes import ScriptedJudge, ToolCallingFakeModel, judged
from tests._conversation_support import claim, plan_reply

NO_ENV_FILE: dict[str, Any] = {"_env_file": None}
BETA_STATEMENT = "The Orchid beta launch is planned for 14 October 2026"


def _offline_settings() -> Settings:
    settings = Settings(**NO_ENV_FILE)
    attention = settings.perception.attention.model_copy(update={"enabled": False})
    perception = settings.perception.model_copy(update={"attention": attention})
    return settings.model_copy(update={"perception": perception})


def _always_reply(window: JudgeWindow) -> TurnJudgement:
    del window
    return judged("reply")


async def _always_met(context: str, reply: str, criterion: str) -> Verdict:
    del context, reply
    return Verdict(met=True, reason=criterion)


class PromptGroundedModel(ToolCallingFakeModel):
    @override
    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        self.seen.append(list(messages))
        text = " ".join(str(message.content) for message in messages)
        question = str(messages[-1].content).casefold()
        if "checklist" in question:
            reply = plan_reply(
                claim("Rani owns the Orchid launch checklist.", "om-orchid-checklist")
            )
        elif "marketing budget" in question:
            reply = "I do not have that figure; I will collect it for Rani."
        elif "beta launch" in question and BETA_STATEMENT in text:
            reply = plan_reply(
                claim(
                    "The beta launch is planned for 14 October 2026.",
                    "om-orchid-beta-launch",
                )
            )
        elif "beta launch" in question:
            reply = "I do not have a confirmed launch date yet."
        else:
            reply = "Noted."
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])


def test_the_versioned_fixture_pins_two_versions_and_covers_every_case() -> None:
    fixture = load_versioned_cases()

    assert [owner_model.version for owner_model in fixture.versions] == [4, 5]
    before = {item.item_id for item in fixture.versions[0].items}
    after = {item.item_id for item in fixture.versions[1].items}
    assert after - before == {"orchid-beta-launch"}
    assert {case.id for case in fixture.cases} == {
        "beta-date",
        "checklist-owner",
        "unapproved-budget",
    }


def test_a_delta_reports_only_the_steps_the_version_moved() -> None:
    delta = VersionDelta(
        case="beta-date",
        before_version=4,
        after_version=5,
        said="Rani, when is the Orchid beta launch?",
        before_reply="I do not have a confirmed launch date yet.",
        after_reply="The beta launch is planned for 14 October 2026.",
        before_passed=True,
        after_passed=True,
    )

    assert delta.reply_changed is True


async def test_replay_answers_from_the_pinned_version_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = PromptGroundedModel(responses=[AIMessage(content="unused")])
    monkeypatch.setattr(assembly, "create_chat_model", lambda _settings: model)
    monkeypatch.setattr(assembly, "create_fallback_chat_model", lambda _settings: None)
    monkeypatch.setattr(assembly, "create_strong_chat_model", lambda _settings: None)
    monkeypatch.setattr(assembly, "build_turn_judge", lambda *_args: ScriptedJudge(_always_reply))
    fixture = load_versioned_cases()

    report = await replay_owner_model_versions(fixture, _offline_settings(), _always_met)

    assert report.severe_failures() == []
    assert [outcome.reply for outcome in report.outcomes[4]["beta-date"]] == [
        "I do not have a confirmed launch date yet."
    ]
    assert [outcome.reply for outcome in report.outcomes[5]["beta-date"]] == [
        "The beta launch is planned for 14 October 2026."
    ]
    deltas = {delta.case: delta for delta in report.deltas()}
    assert deltas["beta-date"].reply_changed is True
    assert deltas["checklist-owner"].reply_changed is False
    assert deltas["unapproved-budget"].reply_changed is False
    assert all(delta.before_passed and delta.after_passed for delta in deltas.values())


@pytest.mark.live
async def test_live_owner_model_versions_move_only_the_designed_answer(tmp_path: Path) -> None:
    settings = Settings()
    try:
        create_chat_model(settings)
        judge = model_judge(create_debrief_chat_model(settings))
    except (FakeYouError, ValueError) as error:
        pytest.skip(str(error))
    fixture = load_versioned_cases()

    report: VersionReplayReport = await replay_owner_model_versions(fixture, settings, judge)

    raw_target = os.environ.get("OWNER_MODEL_VERSIONS_ARTIFACT_DIR", "")
    target = Path(raw_target) if raw_target else tmp_path
    target.mkdir(parents=True, exist_ok=True)
    (target / "owner-model-versions.json").write_text(
        json.dumps(
            {"reply_model": settings.reply.model, **report.as_json()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert report.severe_failures() == []
    deltas = {delta.case: delta for delta in report.deltas()}
    assert deltas["beta-date"].reply_changed is True
    assert deltas["checklist-owner"].before_passed == deltas["checklist-owner"].after_passed
    assert deltas["unapproved-budget"].before_passed == deltas["unapproved-budget"].after_passed
