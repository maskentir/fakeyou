from typing import cast

import pytest
from pydantic import SecretStr

from fakeyou.config.settings import ParticipationDeciderSettings, Settings
from fakeyou.conversation.conversation import LangGraphResponder, MeetingAgent
from fakeyou.conversation.participation_decider import StructuredGeminiDecider
from fakeyou.conversation.participation_graph import DeciderFailure
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.orchestration.assembly import (
    DEFAULT_AGENT_WIRING,
    TurnBehavior,
    _ConversationBuilder,
)


def _responder() -> LangGraphResponder:
    return LangGraphResponder(cast(MeetingAgent, object()), "participation-thread")


def _builder(settings: Settings) -> _ConversationBuilder:
    return _ConversationBuilder(
        settings,
        DEFAULT_AGENT_WIRING,
        TurnBehavior(on_transition=lambda *_args: None),
        Diagnostics(),
    )


def test_build_turns_attaches_gemini_decider_when_configured(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "participation_decider": ParticipationDeciderSettings(api_key=SecretStr("test-key"))
        }
    )
    turns = _builder(configured).build_turns(_responder())
    assert turns.lifecycle.jev is not None
    assert isinstance(turns.decider, StructuredGeminiDecider)


def test_build_turns_rejects_enabled_decider_without_credentials(settings: Settings) -> None:
    keyless = settings.model_copy(
        update={"participation_decider": ParticipationDeciderSettings(enabled=True)}
    )
    with pytest.raises(DeciderFailure, match="decider_no_credentials"):
        _builder(keyless).build_turns(_responder())


def test_build_turns_skips_decider_when_disabled(settings: Settings) -> None:
    disabled = settings.model_copy(
        update={"participation_decider": ParticipationDeciderSettings(enabled=False)}
    )
    turns = _builder(disabled).build_turns(_responder())
    assert turns.lifecycle.jev is None
    assert turns.decider is None
