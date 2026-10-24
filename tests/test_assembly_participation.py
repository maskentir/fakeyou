from typing import cast

import pytest
from pydantic import SecretStr

from fakeyou.config.settings import GoogleReply, Settings, TurnJudgeSettings
from fakeyou.conversation.conversation import MeetingAgent, MeetingResponder
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.conversation.turn_judge import ModelTurnJudge
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.orchestration import assembly
from fakeyou.orchestration.assembly import (
    DEFAULT_AGENT_WIRING,
    TurnBehavior,
    _ConversationBuilder,
)

REAL_BUILD_TURN_JUDGE = assembly.build_turn_judge


def _responder() -> MeetingResponder:
    return MeetingResponder(cast(MeetingAgent, object()), NoHistory())


def _builder(settings: Settings) -> _ConversationBuilder:
    return _ConversationBuilder(
        settings,
        DEFAULT_AGENT_WIRING,
        TurnBehavior(on_transition=lambda *_args: None, twin_names=("Rani",)),
        Diagnostics(),
    )


def test_every_meeting_has_a_turn_judge(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assembly, "build_turn_judge", REAL_BUILD_TURN_JUDGE)
    turns = _builder(settings).build_turns(_responder())

    judging = turns.lifecycle.judging
    assert judging is not None
    assert judging.thresholds.reply == settings.turn_judge.reply_threshold


def test_the_judge_runs_on_the_reply_model_unless_a_judge_model_is_set(
    settings: Settings,
) -> None:
    default = REAL_BUILD_TURN_JUDGE(settings, ("Rani",))
    own = settings.model_copy(
        update={
            "turn_judge": TurnJudgeSettings(
                model=GoogleReply(model="gemini-3.1-flash-lite", api_key=SecretStr("judge-key"))
            )
        }
    )

    assert isinstance(default, ModelTurnJudge)
    assert isinstance(REAL_BUILD_TURN_JUDGE(own, ("Rani",)), ModelTurnJudge)
