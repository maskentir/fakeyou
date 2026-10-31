from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

import fakeyou.orchestration.assembly as assembly_module
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.session import SessionConfig
from fakeyou.conversation.turn_judge import JudgeLine, JudgeWindow, TurnJudgement
from fakeyou.orchestration.assembly import ModelWiring, TurnBehavior


class RecordingJudge:
    def __init__(self) -> None:
        self.windows: list[JudgeWindow] = []

    async def judge(self, window: JudgeWindow) -> TurnJudgement:
        self.windows.append(window)
        return TurnJudgement(
            heard="Rani",
            finished=True,
            backchannel=False,
            addressee="twin",
            action="reply",
            confidence=0.9,
        )


def _config(settings: Settings) -> SessionConfig:
    return SessionConfig(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        meeting_id=SessionId("model-wiring"),
        link="https://meet.google.com/abc-defg-hij",
        twin_name="Wiring Twin",
    )


async def test_the_conversation_answers_on_the_injected_model(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: dict[str, Any] = {}
    marker = FakeMessagesListChatModel(responses=[])

    def capture(model: Any, **_kwargs: Any) -> Any:
        created["model"] = model
        return None

    monkeypatch.setattr(assembly_module, "create_meeting_agent", capture)

    async with assembly_module.open_conversation(
        settings,
        _config(settings),
        behavior=TurnBehavior(on_transition=lambda *_args: None),
        models=ModelWiring(reply=lambda: marker),
    ):
        pass

    assert created["model"] is marker


async def test_the_conversation_judges_with_the_injected_judge(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assembly_module, "create_meeting_agent", lambda *_args, **_kwargs: None)
    judge = RecordingJudge()

    async with assembly_module.open_conversation(
        settings,
        _config(settings),
        behavior=TurnBehavior(on_transition=lambda *_args: None),
        models=ModelWiring(judge=lambda _names: judge),
    ) as turns:
        judging = turns.lifecycle.judging
        assert judging is not None
        await judging.cache.judgement(JudgeWindow((), JudgeLine("ayu", "Rani?")))

    assert len(judge.windows) == 1
    assert judge.windows[0].latest.text == "Rani?"


async def test_without_the_wiring_the_real_factories_are_used(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: dict[str, Any] = {}

    def capture_judge(model_settings: Settings, *_args: Any) -> Any:
        built["judge"] = model_settings
        return None

    monkeypatch.setattr(assembly_module, "create_meeting_agent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(assembly_module, "build_turn_judge", capture_judge)

    async with assembly_module.open_conversation(
        settings,
        _config(settings),
        behavior=TurnBehavior(on_transition=lambda *_args: None),
    ):
        pass

    assert built["judge"] is settings
