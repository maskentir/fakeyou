from pydantic import SecretStr

from fakeyou.config.settings import ParticipationDeciderSettings, Settings
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.orchestration.assembly import (
    DEFAULT_AGENT_WIRING,
    LoopBehavior,
    _ConversationBuilder,
)
from tests._conversation_fakes import FakeResponder


def _builder(settings: Settings) -> _ConversationBuilder:
    return _ConversationBuilder(
        settings,
        DEFAULT_AGENT_WIRING,
        LoopBehavior(on_transition=lambda *_args: None),
        Diagnostics(),
    )


def test_build_loop_attaches_jev_decider_when_configured(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "participation_decider": ParticipationDeciderSettings(
                api_key=SecretStr("test-key"), base_url="https://jev.example"
            )
        }
    )
    loop = _builder(configured).build_loop(FakeResponder())
    assert loop.jev_enabled is True
    assert loop.jev_provider == "jev"


def test_build_loop_skips_jev_decider_without_credentials(settings: Settings) -> None:
    loop = _builder(settings).build_loop(FakeResponder())
    assert loop.jev_enabled is False


def test_build_loop_attaches_gemini_decider_without_base_url(
    settings: Settings,
) -> None:
    configured = settings.model_copy(
        update={
            "participation_decider": ParticipationDeciderSettings(
                provider="gemini",
                model="gemini-3.8-flash",
                api_key=SecretStr("test-key"),
            )
        }
    )
    loop = _builder(configured).build_loop(FakeResponder())
    assert loop.jev_enabled is True
    assert loop.jev_provider == "gemini"
