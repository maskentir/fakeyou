import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import SecretStr

import fakeyou.cli.main as cli_main
import fakeyou.orchestration.assembly as assembly
import fakeyou.orchestration.providers as providers
from fakeyou.catalog.errors import CATALOG, ErrorCode, FakeYouError
from fakeyou.config.settings import (
    CompatibleReply,
    GoogleReply,
    MiniMaxTTS,
    Settings,
)
from fakeyou.conversation.middleware import (
    ChatTurnToolsMiddleware,
    HistoryTrimMiddleware,
    OrphanToolCallMiddleware,
)
from fakeyou.conversation.session import SessionResult
from fakeyou.foundation.greetings import create_greeting_middleware
from fakeyou.foundation.invariants import (
    AGENT_MIC_SOURCE_NAME,
    AGENT_SINK_NAME,
    CAPTURE_SAMPLE_RATE,
    MEETING_MONITOR_SOURCE_NAME,
    MEETING_SINK_NAME,
    PLAYBACK_SAMPLE_RATE,
)
from fakeyou.orchestration.assembly import TurnBehavior
from fakeyou.speech.minimax import MiniMaxSpeechSynthesizer
from fakeyou.speech.registry import build_speech_synthesizer
from tests._check_support import print_admin_check as check_setup
from tests._cli_support import BASE_HOSTS, AudioJoin, with_muse_fallback
from tests._fakes_common import env_file_opt
from tests._runtime_fakes import SessionRun


def test_main_exits_2_on_input_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["fakeyou", "join", "not-a-link"])
    with pytest.raises(SystemExit) as excinfo:
        cli_main.main()
    assert excinfo.value.code == 2
    assert "meet.google.com" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "handler", "expected"),
    [
        (
            ["memory-reembed", "--twin-id", "t"],
            "reembed_memory",
            {"twin_id": "t"},
        ),
        (
            ["memory-forget", "--meeting-id", "m1", "--keep-record"],
            "forget_meeting_memory",
            {"meeting_id": "m1", "keep_record": True},
        ),
        (
            ["report", "--meeting-id", "m1"],
            "show_session_report",
            {"meeting_id": "m1"},
        ),
        (
            ["chat", "hello", "--thread", "t1", "--memory"],
            "chat_via_api",
            {"message": "hello", "thread": "t1", "memory": True},
        ),
    ],
)
def test_main_dispatches_parsed_options_to_the_command_handler(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], handler: str, expected: dict[str, Any]
) -> None:
    calls: list[dict[str, Any]] = []

    def record(**kwargs: Any) -> None:
        calls.append(kwargs)

    async def record_async(**kwargs: Any) -> None:
        calls.append(kwargs)

    sync_handlers: set[str] = set()
    monkeypatch.setattr(cli_main, handler, record if handler in sync_handlers else record_async)
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert call.pop("settings") is not None
    assert {key: call[key] for key in expected} == expected


def test_compose_pulse_defaults_match_audio_constants() -> None:
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text()
    assert f"PULSE_SOURCE: ${{PULSE_SOURCE:-{MEETING_MONITOR_SOURCE_NAME}}}" in compose
    assert f"PULSE_SINK: ${{PULSE_SINK:-{AGENT_SINK_NAME}}}" in compose


def test_compose_worker_reaches_neo4j_service_and_example_profiles() -> None:
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text()
    assert "env_file: .env" in compose
    assert "FAKEYOU_MEMORY__STORE__URI: ${FAKEYOU_COMPOSE_NEO4J_URI:-bolt://neo4j:7687}" in compose
    remapped = [
        line
        for line in compose.splitlines()
        if line.strip().startswith("FAKEYOU_") and "${FAKEYOU_COMPOSE_WORKER_" in line
    ]
    assert remapped == []
    assert "./examples:/workspace/examples:ro" in compose


def test_env_example_declares_pulse_overrides() -> None:
    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
    assert f"PULSE_SOURCE={MEETING_MONITOR_SOURCE_NAME}" in example.splitlines()
    assert f"PULSE_SINK={AGENT_SINK_NAME}" in example.splitlines()


def test_dockerfile_pulse_defaults_match_audio_constants() -> None:
    dockerfile = (Path(__file__).resolve().parent.parent / "docker/audio/Dockerfile").read_text()
    assert f"PULSE_SOURCE={MEETING_MONITOR_SOURCE_NAME}" in dockerfile
    assert f"PULSE_SINK={AGENT_SINK_NAME}" in dockerfile


def test_default_pa_pins_audio_constants() -> None:
    default_pa = (Path(__file__).resolve().parent.parent / "docker/audio/default.pa").read_text()
    assert f"sink_name={MEETING_SINK_NAME} rate={CAPTURE_SAMPLE_RATE}" in default_pa
    assert f"sink_name={AGENT_SINK_NAME} rate={PLAYBACK_SAMPLE_RATE}" in default_pa
    assert f"source_name={AGENT_MIC_SOURCE_NAME} master={AGENT_SINK_NAME}.monitor" in default_pa
    assert f"set-default-sink {MEETING_SINK_NAME}" in default_pa
    assert f"set-default-source {AGENT_MIC_SOURCE_NAME}" in default_pa


@pytest.mark.asyncio
async def test_join_audio_wires_journal_and_preflight_hosts(
    audio_join: AudioJoin,
    session_run: SessionRun,
    settings: Settings,
) -> None:
    captured = session_run

    async def probe(_config: Any, **kwargs: Any) -> SessionResult:
        kwargs["open_audio"].keywords["diagnostics"].journal.record("wiring_probe", source="audio")
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    session_run.effect = probe
    await audio_join()

    rows = [
        json.loads(line)
        for line in (settings.paths.journal_dir / "meeting-9.jsonl").read_text().splitlines()
    ]
    [probed] = [row for row in rows if row["event"] == "wiring_probe"]
    assert probed["meeting_id"] == "meeting-9"
    assert captured["preflight_hosts"] == BASE_HOSTS
    assert captured["preflight_budget_seconds"] == 5.0


@pytest.mark.asyncio
async def test_join_without_audio_skips_preflight(
    session_run: SessionRun,
    join: Callable[..., Any],
) -> None:
    captured = session_run

    session_run.result.update(
        {"phase": "ended", "terminal_reason": "stopped_by_owner", "detail": "ok"}
    )

    await join(
        "https://meet.google.com/abc-defg-hij",
    )

    assert captured["preflight_hosts"] is None


def record_chat_model_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        providers, "init_chat_model", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    return calls


def test_main_reports_unavailable_memory_with_infra_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from fakeyou.observability.provider_faults import ProviderFault

    async def unavailable(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise ProviderFault("memory", "unavailable")

    monkeypatch.setattr(cli_main, "add_owner_note", unavailable)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "memory-add", "x", "--twin-id", "t"])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == 3
    assert "memory unavailable" in capsys.readouterr().err


def test_chat_model_uses_role_configuration(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_chat_model_calls(monkeypatch)
    settings.reply = CompatibleReply(
        model="muse-spark-1.3",
        base_url="https://api.meta.ai/v1",
        api_key=SecretStr("meta-key"),
        reasoning_effort="minimal",
    )
    providers.create_chat_model(settings)
    assert calls == [
        (
            ("muse-spark-1.3",),
            {
                "model_provider": "openai",
                "api_key": "meta-key",
                "timeout": 15.0,
                "max_retries": 2,
                "base_url": "https://api.meta.ai/v1",
                "reasoning_effort": "minimal",
            },
        )
    ]


def test_minimal_reasoning_is_refused_for_a_gemini_model_without_it(settings: Settings) -> None:
    settings.reply = GoogleReply(
        model="gemini-3.8-flash", api_key=SecretStr("google-key"), reasoning_effort="minimal"
    )

    with pytest.raises(FakeYouError, match="does not support minimal reasoning effort"):
        providers.create_chat_model(settings)


def test_minimal_reasoning_is_kept_for_a_gemini_model_that_supports_it(
    settings: Settings,
) -> None:
    settings.reply = GoogleReply(
        model="gemini-3.1-flash-lite", api_key=SecretStr("google-key"), reasoning_effort="minimal"
    )

    model = providers.create_chat_model(settings)

    assert isinstance(model, ChatGoogleGenerativeAI)
    assert model.reasoning_effort == "minimal"


def test_chat_model_forwards_google_reasoning_effort(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_chat_model_calls(monkeypatch)
    settings.reply = GoogleReply(
        model="gemini-3.8-flash",
        api_key=SecretStr("google-key"),
        reasoning_effort="low",
    )
    providers.create_chat_model(settings)
    assert calls == [
        (
            ("gemini-3.8-flash",),
            {
                "model_provider": "google_genai",
                "api_key": "google-key",
                "timeout": 15.0,
                "max_retries": 2,
                "base_url": "https://generativelanguage.googleapis.com",
                "reasoning_effort": "low",
            },
        )
    ]


def test_chat_model_passes_provider_request_fields_as_extra_body(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_chat_model_calls(monkeypatch)
    settings.reply = CompatibleReply(
        model="MiniMax-M3",
        base_url="https://api.minimax.io/v1",
        api_key=SecretStr("minimax-key"),
        extra_body={"reasoning_split": True},
        max_retries=0,
    )
    settings.reply_fallback = settings.reply.model_copy(update={"model": "MiniMax-M2.5-highspeed"})
    providers.create_chat_model(settings)
    providers.create_fallback_chat_model(settings)
    assert [kwargs.get("extra_body") for _args, kwargs in calls] == [
        {"reasoning_split": True},
        {"reasoning_split": True},
    ]


def test_settings_read_extra_body_as_json(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    del settings
    monkeypatch.setenv("FAKEYOU_REPLY__PROVIDER", "openai_compatible")
    monkeypatch.setenv("FAKEYOU_REPLY__BASE_URL", "https://api.minimax.io/v1")
    monkeypatch.setenv("FAKEYOU_REPLY__EXTRA_BODY", '{"reasoning_split": true}')
    reply = Settings(**env_file_opt(None)).reply
    assert isinstance(reply, CompatibleReply)
    assert reply.extra_body == {"reasoning_split": True}


def test_chat_model_uses_its_own_key_and_explicit_provider(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_chat_model_calls(monkeypatch)
    providers.create_chat_model(settings)
    assert calls == [
        (
            ("model",),
            {
                "api_key": "test-key",
                "model_provider": "openai",
                "timeout": 15.0,
                "max_retries": 2,
                "base_url": "https://api.openai.com/v1",
            },
        )
    ]


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1:8080/v1", "https://api.meta.ai:8443/v1", "https://api.anthropic.com"],
)
def test_preflight_hosts_follow_the_configured_chat_endpoint(
    settings: Settings, endpoint: str
) -> None:
    settings.reply.base_url = endpoint
    assert providers.preflight_hosts(settings) == (*BASE_HOSTS, endpoint)


def test_minimax_speech_needs_its_own_key_and_is_probed_before_join(settings: Settings) -> None:
    settings.tts = MiniMaxTTS()
    settings.reply = CompatibleReply(model="MiniMax-M3", base_url="https://api.minimax.io/v1")
    with pytest.raises(FakeYouError, match="FAKEYOU_TTS__API_KEY") as caught:
        build_speech_synthesizer(settings.tts, language=settings.speech.language)
    assert caught.value.code is ErrorCode.CONFIG_SECRET_MISSING
    settings.tts.api_key = SecretStr("minimax-key")
    assert isinstance(
        build_speech_synthesizer(settings.tts, language=settings.speech.language),
        MiniMaxSpeechSynthesizer,
    )
    assert providers.preflight_hosts(settings) == (
        "https://api.minimax.io/v1/t2a_v2",
        "wss://api.meta.ai/v1/asr/realtime",
        "https://api.minimax.io/v1",
    )


def test_openai_speech_requires_its_own_key(settings: Settings) -> None:
    settings.tts.api_key = None
    with pytest.raises(FakeYouError, match="FAKEYOU_TTS__API_KEY") as caught:
        build_speech_synthesizer(settings.tts, language=settings.speech.language)
    assert caught.value.code is ErrorCode.CONFIG_SECRET_MISSING


def test_check_reports_the_speech_provider_defaults(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.tts = MiniMaxTTS()
    check_setup(settings)
    assert json.loads(capsys.readouterr().out)["tts"] == settings.tts.model_dump(mode="json")


def test_check_reports_models_per_role(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    check_setup(settings)
    report = capsys.readouterr().out
    assert json.loads(report) == settings.model_dump(mode="json")
    assert "test-key" not in report
    assert "memory-key" not in report


def test_primary_and_fallback_use_explicit_retry_budgets(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_chat_model_calls(monkeypatch)
    settings = with_muse_fallback(settings)
    providers.create_chat_model(settings)
    providers.create_fallback_chat_model(settings)
    assert [kwargs["max_retries"] for _args, kwargs in calls] == [0, 1]
    assert [args for args, _kwargs in calls] == [("model",), ("muse-spark-1.2",)]


def test_fallback_chat_model_is_optional(settings: Settings) -> None:
    assert providers.create_fallback_chat_model(settings) is None


async def test_conversation_adds_model_fallback_middleware_when_configured(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from langchain.agents.middleware import ModelFallbackMiddleware

    created: dict[str, Any] = {}

    def fake_agent(_model: Any, **kwargs: Any) -> SimpleNamespace:
        created.update(kwargs)
        return SimpleNamespace(name="agent")

    monkeypatch.setattr(assembly, "create_meeting_agent", fake_agent)
    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: SimpleNamespace())
    settings = with_muse_fallback(settings)
    config = SimpleNamespace(
        owner_id="o", twin_id="t", meeting_id="m", participant_policy_enabled=False
    )

    async with assembly.open_conversation(
        settings,
        cast(Any, config),
        behavior=TurnBehavior(on_transition=lambda *_args: None),
    ):
        pass

    greeting_type = type(create_greeting_middleware("id"))
    assert [type(item) for item in created["middleware"]] == [
        greeting_type,
        ModelFallbackMiddleware,
        HistoryTrimMiddleware,
        OrphanToolCallMiddleware,
        ChatTurnToolsMiddleware,
    ]


async def test_open_conversation_wires_sentence_streaming_flag(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.conversation.conversation import LangGraphResponder

    seen: dict[str, Any] = {}

    class StubLifecycle:
        async def close(self) -> None:
            pass

        def configure_memory(self, memory: Any) -> None:
            seen["memory"] = memory

    class StubTurns:
        lifecycle = StubLifecycle()

    def fake_turns(responder: Any, **kwargs: Any) -> StubTurns:
        seen["responder"] = responder
        seen["kwargs"] = kwargs
        return StubTurns()

    monkeypatch.setattr(
        assembly, "create_meeting_agent", lambda *_args, **_kwargs: SimpleNamespace(name="agent")
    )
    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(assembly, "build_meeting_turns", fake_turns)
    config = SimpleNamespace(
        owner_id="o", twin_id="t", meeting_id="m", participant_policy_enabled=False
    )

    async with assembly.open_conversation(
        settings,
        cast(Any, config),
        behavior=TurnBehavior(on_transition=lambda *_args: None),
    ):
        pass
    assert type(seen["responder"]) is LangGraphResponder
    assert seen["kwargs"]["generation"].sentence_streaming is True

    settings.conversation.sentence_streaming = True
    async with assembly.open_conversation(
        settings,
        cast(Any, config),
        behavior=TurnBehavior(on_transition=lambda *_args: None),
    ):
        pass
    assert type(seen["responder"]) is LangGraphResponder
    assert seen["responder"].max_sentences == settings.conversation.max_spoken_sentences
    assert seen["responder"].language == settings.speech.language
    assert seen["kwargs"]["generation"].sentence_streaming is True


def test_preflight_probes_the_fallback_endpoint(settings: Settings) -> None:
    settings = with_muse_fallback(settings)
    assert settings.reply_fallback is not None
    settings.reply_fallback.base_url = "https://fallback.example.com:8443/v1"
    assert providers.preflight_hosts(settings) == (
        *BASE_HOSTS,
        "https://fallback.example.com:8443/v1",
    )


def test_check_reports_the_fallback_model(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = with_muse_fallback(settings)
    assert settings.reply_fallback is not None
    check_setup(settings)
    assert json.loads(capsys.readouterr().out)[
        "reply_fallback"
    ] == settings.reply_fallback.model_dump(mode="json")


def test_check_reports_no_fallback_by_default(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    check_setup(settings)
    assert json.loads(capsys.readouterr().out)["reply_fallback"] is None


def test_ctrl_c_is_reported_as_an_operation_interruption(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(**_kwargs: Any) -> NoReturn:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main, "show_session_report", interrupted)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "report", "--meeting-id", "m1"])

    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == CATALOG[ErrorCode.OPERATION_INTERRUPTED].exit_code
    assert "operation interrupted" in capsys.readouterr().err


def test_ctrl_c_during_settings_is_reported_as_an_operation_interruption(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted_settings(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_main, "client_settings_from_env", interrupted_settings)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "report", "--meeting-id", "m1"])

    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == CATALOG[ErrorCode.OPERATION_INTERRUPTED].exit_code
    assert "operation interrupted" in capsys.readouterr().err
