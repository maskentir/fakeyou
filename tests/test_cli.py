import asyncio
import json
import signal
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ToolErrorMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

import fakeyou.cli as cli
import fakeyou.memory as memory_module
from fakeyou.agent import DEFAULT_PERSONA, create_meeting_agent
from fakeyou.audio import (
    AGENT_MIC_SOURCE_NAME,
    AGENT_SINK_NAME,
    CAPTURE_SAMPLE_RATE,
    MEETING_MONITOR_SOURCE_NAME,
    MEETING_SINK_NAME,
    PLAYBACK_SAMPLE_RATE,
)
from fakeyou.conversation import ConversationLoop, conversation_thread_id
from fakeyou.persona import PersonaProfile, build_persona
from fakeyou.playback import PlaybackEvent
from fakeyou.record import meeting_record_path, read_meeting_record


class FakeMemory:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.factory_thread = threading.get_ident()

    async def build_indices_and_constraints(self) -> None:
        self.events.append("build")
        if self.fail:
            raise RuntimeError("index setup failed")

    async def close(self) -> None:
        self.events.append("close")


def fake_settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        openai_api_key=SecretStr("test-key"),
        chat_model="fake:model",
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        memory_group="test-group",
        neo4j_uri="bolt://localhost:7687",
        neo4j_user="neo4j",
        neo4j_password=SecretStr("password"),
        headless=True,
        cloakbrowser_license_key=None,
        owner_id="settings-owner",
        twin_id="settings-twin",
        twin_name="Settings Twin",
        lobby_timeout_seconds=300.0,
        session_duration_seconds=7200.0,
        journal_dir=tmp_path / "journals",
        meeting_record_dir=tmp_path / "meetings",
        chat_base_url=None,
        chat_api_key=None,
        chat_reasoning_effort=None,
        memory_llm_model="memory-model",
        memory_small_model="memory-small",
        memory_embedding_model="memory-embedding",
        stt_model="muse-voice-transcribe-1.0",
    )


@pytest.mark.asyncio
async def test_chat_waits_for_memory_ready_before_exposing_tools(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    main_thread = threading.get_ident()
    memory = FakeMemory(events)

    def create_memory(_config):
        events.append("construct")
        memory.factory_thread = threading.get_ident()
        return memory

    def create_tools(_memory, *, group_id):
        events.append(f"tools:{group_id}")
        return []

    class FakeAgent:
        async def ainvoke(self, *_args, **_kwargs):
            events.append("invoke")
            return {"messages": [SimpleNamespace(content="ok")]}

    def create_agent(*_args, **_kwargs):
        events.append("agent")
        return FakeAgent()

    monkeypatch.setattr(cli, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(memory_module, "create_graphiti_memory", create_memory)
    monkeypatch.setattr(cli, "create_memory_tools", create_tools)
    monkeypatch.setattr(cli, "create_meeting_agent", create_agent)

    await cli.chat("hello", "thread", False, True, fake_settings(tmp_path))

    assert memory.factory_thread != main_thread
    assert events == ["construct", "tools:test-group", "agent", "invoke", "close"]


@pytest.mark.asyncio
async def test_memory_init_builds_indexes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[str] = []
    memory = FakeMemory(events)

    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config: memory)

    await cli.initialize_memory(fake_settings(tmp_path))

    assert events == ["build", "close"]
    assert "Graphiti indexes initialized." in capsys.readouterr().out


@pytest.mark.asyncio
async def test_memory_init_closes_memory_when_index_setup_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    memory = FakeMemory(events, fail=True)

    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config: memory)

    with pytest.raises(RuntimeError, match="index setup failed"):
        await cli.initialize_memory(fake_settings(tmp_path))

    assert events == ["build", "close"]


@pytest.mark.asyncio
async def test_join_builds_session_config_and_prints_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict = {}

    async def fake_run(config, **kwargs):
        captured["config"] = config
        captured["kwargs"] = kwargs
        return SimpleNamespace(phase="ended", terminal_reason="denied_by_host", detail="nope")

    monkeypatch.setattr(cli, "run_meeting_session", fake_run)

    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    await cli.join_meeting(
        "https://meet.google.com/abc-defg-hij",
        twin_name="Twin",
        owner_id="owner-1",
        twin_id="twin-1",
        meeting_id="meeting-1",
        lobby_timeout=60.0,
        duration=600.0,
        profile_dir="/tmp/custom-profile",
        settings=fake_settings(tmp_path),
    )

    config = captured["config"]
    assert config.link == "https://meet.google.com/abc-defg-hij"
    assert config.twin_name == "Twin"
    assert config.owner_id == "owner-1"
    assert config.twin_id == "twin-1"
    assert config.meeting_id == "meeting-1"
    assert config.lobby_timeout_seconds == 60.0
    assert config.session_duration_seconds == 600.0
    assert config.browser_profile_dir == Path("/tmp/custom-profile")
    assert config.headless is True
    assert "stop" in captured["kwargs"]
    printed = capsys.readouterr().out
    assert '"meeting_id": "meeting-1"' in printed
    assert '"terminal_reason": "denied_by_host"' in printed
    assert {sig: signal.getsignal(sig) for sig in previous_handlers} == previous_handlers


@pytest.mark.asyncio
async def test_join_falls_back_to_settings_and_generates_meeting_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict = {}

    async def fake_run(config, **kwargs):
        captured["config"] = config
        return SimpleNamespace(phase="ended", terminal_reason="owner_stop", detail="ok")

    monkeypatch.setattr(cli, "run_meeting_session", fake_run)

    await cli.join_meeting(
        "https://meet.google.com/abc-defg-hij",
        twin_name=None,
        owner_id=None,
        twin_id=None,
        meeting_id=None,
        lobby_timeout=None,
        duration=None,
        profile_dir=None,
        settings=fake_settings(tmp_path),
    )

    config = captured["config"]
    assert config.twin_name == "Settings Twin"
    assert config.owner_id == "settings-owner"
    assert config.twin_id == "settings-twin"
    assert config.browser_profile_dir == Path(".runtime/profiles/settings-twin")
    assert config.lobby_timeout_seconds == 300.0
    assert config.session_duration_seconds == 7200.0
    assert len(config.meeting_id) == 12
    assert f'"meeting_id": "{config.meeting_id}"' in capsys.readouterr().out


@pytest.mark.parametrize(
    "terminal_reason,exit_code",
    [
        ("owner_stop", 0),
        ("denied_by_host", 0),
        ("meeting_ended", 0),
        ("duration_limit", 0),
        ("left_alone", 0),
        ("browser_failure", 1),
        ("audio_failure", 1),
        ("conversation_failure", 1),
        ("infra_failure", 3),
    ],
)
async def test_join_exit_code_matches_terminal_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    terminal_reason: str,
    exit_code: int,
) -> None:
    async def fake_run(config, **kwargs):
        return SimpleNamespace(phase="ended", terminal_reason=terminal_reason, detail="done")

    monkeypatch.setattr(cli, "run_meeting_session", fake_run)

    if exit_code == 0:
        await cli.join_meeting(
            "https://meet.google.com/abc-defg-hij",
            twin_name=None,
            owner_id=None,
            twin_id=None,
            meeting_id="meeting-1",
            lobby_timeout=None,
            duration=None,
            profile_dir=None,
            settings=fake_settings(tmp_path),
        )
    else:
        with pytest.raises(SystemExit) as caught:
            await cli.join_meeting(
                "https://meet.google.com/abc-defg-hij",
                twin_name=None,
                owner_id=None,
                twin_id=None,
                meeting_id="meeting-1",
                lobby_timeout=None,
                duration=None,
                profile_dir=None,
                settings=fake_settings(tmp_path),
            )
        assert caught.value.code == exit_code

    captured = capsys.readouterr()
    assert f'"exit_code": {exit_code}' in captured.err
    assert '"event": "cli_exit"' in captured.err
    assert f'"terminal_reason": "{terminal_reason}"' in captured.out


async def test_join_missing_terminal_reason_exits_1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_run(config, **kwargs):
        return SimpleNamespace(phase="ended", terminal_reason=None, detail="unknown")

    monkeypatch.setattr(cli, "run_meeting_session", fake_run)

    with pytest.raises(SystemExit) as caught:
        await cli.join_meeting(
            "https://meet.google.com/abc-defg-hij",
            twin_name=None,
            owner_id=None,
            twin_id=None,
            meeting_id="meeting-1",
            lobby_timeout=None,
            duration=None,
            profile_dir=None,
            settings=fake_settings(tmp_path),
        )
    assert caught.value.code == 1


async def test_join_result_carries_terminal_fault_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = {"faults": {"kinds": [{"kind": "timeout", "count": 2}], "total": 2}}

    async def fake_run(config, **kwargs):
        return SimpleNamespace(
            phase="ended", terminal_reason="owner_stop", detail="ok", faults=report
        )

    monkeypatch.setattr(cli, "run_meeting_session", fake_run)

    await cli.join_meeting(
        "https://meet.google.com/abc-defg-hij",
        twin_name=None,
        owner_id=None,
        twin_id=None,
        meeting_id="meeting-1",
        lobby_timeout=None,
        duration=None,
        profile_dir=None,
        settings=fake_settings(tmp_path),
    )

    assert '"timeout"' in capsys.readouterr().out


async def test_join_without_audio_still_wires_faults_and_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fakeyou.faults import FaultReporter
    from fakeyou.journal import ProviderJournal

    captured: dict = {}

    async def fake_run(config, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(phase="ended", terminal_reason="owner_stop", detail="ok")

    monkeypatch.setattr(cli, "run_meeting_session", fake_run)

    await cli.join_meeting(
        "https://meet.google.com/abc-defg-hij",
        twin_name=None,
        owner_id=None,
        twin_id=None,
        meeting_id="meeting-1",
        lobby_timeout=None,
        duration=None,
        profile_dir=None,
        settings=fake_settings(tmp_path),
    )

    assert isinstance(captured["faults"], FaultReporter)
    assert isinstance(captured["journal"], ProviderJournal)
    assert "preflight_hosts" not in captured
    assert (tmp_path / "journals" / "meeting-1.jsonl").is_file()


@pytest.mark.parametrize("audio_enabled,speech", [(False, "hello"), (True, " ")])
async def test_join_rejects_invalid_speech_before_browser_start(tmp_path, audio_enabled, speech):
    with pytest.raises(ValueError, match="--speak"):
        await cli.join_meeting(
            "https://meet.google.com/abc-defg-hij",
            twin_name=None,
            owner_id=None,
            twin_id=None,
            meeting_id=None,
            lobby_timeout=None,
            duration=None,
            profile_dir=None,
            settings=fake_settings(tmp_path),
            audio_enabled=audio_enabled,
            initial_speech=speech,
        )


def test_cli_error_is_value_error() -> None:
    assert issubclass(cli.CliError, ValueError)


@pytest.mark.parametrize(
    "link",
    [
        "",
        "not-a-url",
        "http://meet.google.com/abc-defg-hij",
        "https://example.com/abc-defg-hij",
        "https://meet.google.com/",
    ],
)
async def test_join_rejects_invalid_link(tmp_path, link) -> None:
    with pytest.raises(cli.CliError, match="meet.google.com"):
        await cli.join_meeting(
            link,
            twin_name=None,
            owner_id=None,
            twin_id=None,
            meeting_id=None,
            lobby_timeout=None,
            duration=None,
            profile_dir=None,
            settings=fake_settings(tmp_path),
        )


@pytest.mark.parametrize(
    "lobby_timeout,duration",
    [(0.0, None), (-1.0, None), (float("nan"), None), (None, 0.0), (None, -5.0)],
)
async def test_join_rejects_nonpositive_timeouts(tmp_path, lobby_timeout, duration) -> None:
    with pytest.raises(cli.CliError, match="positive"):
        await cli.join_meeting(
            "https://meet.google.com/abc-defg-hij",
            twin_name=None,
            owner_id=None,
            twin_id=None,
            meeting_id=None,
            lobby_timeout=lobby_timeout,
            duration=duration,
            profile_dir=None,
            settings=fake_settings(tmp_path),
        )


async def test_join_audio_precondition_raises_cli_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PULSE_SOURCE", "wrong-source")
    with pytest.raises(cli.CliError, match="--audio"):
        await cli.join_meeting(
            "https://meet.google.com/abc-defg-hij",
            twin_name=None,
            owner_id=None,
            twin_id=None,
            meeting_id=None,
            lobby_timeout=None,
            duration=None,
            profile_dir=None,
            settings=fake_settings(tmp_path),
            audio_enabled=True,
        )


@pytest.mark.parametrize("thread", ["", "bad thread", "bad:thread", "x" * 129])
async def test_chat_rejects_invalid_thread(monkeypatch, tmp_path, thread) -> None:
    monkeypatch.setattr(
        cli,
        "init_chat_model",
        lambda *_args, **_kwargs: pytest.fail("thread must be validated first"),
    )
    with pytest.raises(cli.CliError, match="thread"):
        await cli.chat("hello", thread, False, False, fake_settings(tmp_path))


def test_main_exits_1_on_cli_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["fakeyou", "join", "not-a-link"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 1
    assert "meet.google.com" in capsys.readouterr().err


async def test_join_headless_override(monkeypatch, tmp_path) -> None:
    captured: dict = {}

    async def fake_run(config, **kwargs):
        captured["config"] = config
        return SimpleNamespace(phase="ended", terminal_reason="owner_stop", detail="ok")

    monkeypatch.setattr(cli, "run_meeting_session", fake_run)
    settings = fake_settings(tmp_path)
    await cli.join_meeting(
        "https://meet.google.com/abc-defg-hij",
        twin_name=None,
        owner_id=None,
        twin_id=None,
        meeting_id=None,
        lobby_timeout=None,
        duration=None,
        profile_dir=None,
        settings=settings,
        headless=False,
    )
    assert captured["config"].headless is False
    settings.headless = False
    await cli.join_meeting(
        "https://meet.google.com/abc-defg-hij",
        twin_name=None,
        owner_id=None,
        twin_id=None,
        meeting_id=None,
        lobby_timeout=None,
        duration=None,
        profile_dir=None,
        settings=settings,
        headless=True,
    )
    assert captured["config"].headless is True
    await cli.join_meeting(
        "https://meet.google.com/abc-defg-hij",
        twin_name=None,
        owner_id=None,
        twin_id=None,
        meeting_id=None,
        lobby_timeout=None,
        duration=None,
        profile_dir=None,
        settings=settings,
    )
    assert captured["config"].headless is False


@pytest.mark.parametrize("flag", ["--owner-id", "--owner"])
def test_join_owner_flags(monkeypatch, flag) -> None:
    captured: dict = {}

    async def fake_join(link, **kwargs):
        captured["link"] = link
        captured.update(kwargs)

    monkeypatch.setattr(cli, "join_meeting", fake_join)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "join", "https://meet.google.com/abc-defg-hij", flag, "owner-9"],
    )
    cli.main()
    assert captured["owner_id"] == "owner-9"


@pytest.mark.parametrize("argv,expected", [(["--headless"], True), ([], None)])
def test_join_headless_flag(monkeypatch, argv, expected) -> None:
    captured: dict = {}

    async def fake_join(link, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli, "join_meeting", fake_join)
    monkeypatch.setattr(
        sys, "argv", ["fakeyou", "join", "https://meet.google.com/abc-defg-hij", *argv]
    )
    cli.main()
    assert captured["headless"] is expected


def test_compose_pulse_defaults_match_audio_constants() -> None:
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text()
    assert f"PULSE_SOURCE: ${{PULSE_SOURCE:-{MEETING_MONITOR_SOURCE_NAME}}}" in compose
    assert f"PULSE_SINK: ${{PULSE_SINK:-{AGENT_SINK_NAME}}}" in compose


def test_compose_worker_reaches_neo4j_service_and_example_profiles() -> None:
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text()
    assert "FAKEYOU_NEO4J_URI: ${FAKEYOU_WORKER_NEO4J_URI:-bolt://neo4j:7687}" in compose
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


def _enable_audio_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("PULSE_SOURCE", MEETING_MONITOR_SOURCE_NAME)
    monkeypatch.setenv("PULSE_SINK", AGENT_SINK_NAME)
    monkeypatch.setattr(cli, "find_spec", lambda name: SimpleNamespace())


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    async def fake_run(config, **kwargs):
        captured["config"] = config
        captured.update(kwargs)
        return SimpleNamespace(phase="ended", terminal_reason="owner_stop", detail="ok")

    monkeypatch.setattr(cli, "run_meeting_session", fake_run)
    return captured


async def _join_with_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides):
    settings = fake_settings(tmp_path)
    settings.model_api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    monkeypatch.setattr(cli, "init_chat_model", lambda *args, **kwargs: SimpleNamespace())

    @asynccontextmanager
    async def fake_saver(value):
        yield SimpleNamespace()

    monkeypatch.setattr(cli.AsyncSqliteSaver, "from_conn_string", fake_saver)
    captured = _capture_run(monkeypatch)
    arguments = {
        "twin_name": None,
        "owner_id": None,
        "twin_id": None,
        "meeting_id": "meeting-9",
        "lobby_timeout": None,
        "duration": None,
        "profile_dir": None,
        "settings": settings,
        "audio_enabled": True,
    }
    arguments.update(overrides)
    await cli.join_meeting("https://meet.google.com/abc-defg-hij", **arguments)
    return captured


@pytest.mark.asyncio
async def test_join_audio_builds_conversation_with_meeting_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: dict = {}

    def fake_agent(model, *, tools=(), checkpointer=None, **kwargs):
        created["tools"] = list(tools)
        created["checkpointer"] = checkpointer
        created.update(kwargs)
        return SimpleNamespace(name="agent")

    monkeypatch.setattr(cli, "create_meeting_agent", fake_agent)
    captured = await _join_with_audio(tmp_path, monkeypatch, conversation_enabled=True)

    loop = captured["conversation"]
    assert isinstance(loop, ConversationLoop)
    assert loop.phase == "closed"
    assert created["tools"] == []
    assert created["checkpointer"] is not None
    assert created["persona"] == DEFAULT_PERSONA
    assert created["middleware"] == []


@pytest.mark.asyncio
async def test_join_audio_transcript_reaches_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    agent_calls: list = []

    class FakeAgent:
        async def ainvoke(self, request, config=None):
            agent_calls.append(config)
            return {"messages": [AIMessage(content="Balasan.")]}

    monkeypatch.setattr(cli, "create_meeting_agent", lambda *args, **kwargs: FakeAgent())
    captured = {}

    def capture_run(monkeypatch):
        async def run(config, **kwargs):
            captured.update(kwargs)
            loop = kwargs["conversation"]
            loop.bind(SimpleNamespace(speak=lambda text: asyncio.sleep(0)))
            frame = SimpleNamespace(
                id="t1",
                text="Halo.",
                finalized=True,
                user_id="",
                timestamp="2026-09-28T00:00:00+00:00",
            )
            await kwargs["open_audio"].keywords["on_transcript"](frame)
            await loop.wait_idle()
            return SimpleNamespace(phase="ended", terminal_reason="owner_stop", detail="ok")

        monkeypatch.setattr(cli, "run_meeting_session", run)
        return captured

    monkeypatch.setattr(sys.modules[__name__], "_capture_run", capture_run)
    await _join_with_audio(tmp_path, monkeypatch, conversation_enabled=True)

    assert [event.text for event in captured["conversation"].events] == ["Halo."]
    assert agent_calls[0]["configurable"]["thread_id"] == conversation_thread_id(
        "settings-owner", "settings-twin", "meeting-9"
    )
    rows = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{"')
    ]
    [final] = [row for row in rows if row["event"] == "transcript_final"]
    assert (final["text"], final["participant"], final["meeting_id"]) == (
        "Halo.",
        "unknown",
        "meeting-9",
    )
    assert final["at"]
    entries = read_meeting_record(meeting_record_path(tmp_path / "meetings", "meeting-9"))
    assert [(entry.kind, entry.text) for entry in entries] == [("transcript", "Halo.")]


@pytest.mark.asyncio
async def test_join_conversation_transitions_carry_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    class FakeAgent:
        async def ainvoke(self, request, config=None):
            return {"messages": [AIMessage(content="Balasan.")]}

    monkeypatch.setattr(cli, "create_meeting_agent", lambda *args, **kwargs: FakeAgent())

    def capture_run(monkeypatch):
        async def run(config, **kwargs):
            loop = kwargs["conversation"]
            loop.bind(SimpleNamespace(speak=lambda text: asyncio.sleep(0)))
            frame = SimpleNamespace(
                id="t1",
                text="Halo.",
                finalized=True,
                user_id="",
                timestamp="2026-09-28T00:00:00+00:00",
            )
            await kwargs["open_audio"].keywords["on_transcript"](frame)
            await loop.wait_idle()
            return SimpleNamespace(phase="ended", terminal_reason="owner_stop", detail="ok")

        monkeypatch.setattr(cli, "run_meeting_session", run)
        return {}

    monkeypatch.setattr(sys.modules[__name__], "_capture_run", capture_run)
    await _join_with_audio(tmp_path, monkeypatch, conversation_enabled=True)

    transitions = [
        line
        for line in capsys.readouterr().out.splitlines()
        if '"event":"conversation_transition"' in line
    ]
    assert len(transitions) == 3
    assert all(json.loads(line)["at"] for line in transitions)


async def test_audio_only_does_not_construct_responding_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli, "create_meeting_agent", lambda *args, **kwargs: pytest.fail("agent must be opt-in")
    )
    captured = await _join_with_audio(tmp_path, monkeypatch)
    assert captured["conversation"] is None


async def test_conversation_option_requires_audio(tmp_path):
    with pytest.raises(cli.CliError, match="--converse requires --audio"):
        await cli.join_meeting(
            "https://meet.google.com/abc-defg-hij",
            twin_name=None,
            owner_id=None,
            twin_id=None,
            meeting_id=None,
            lobby_timeout=None,
            duration=None,
            profile_dir=None,
            settings=fake_settings(tmp_path),
            conversation_enabled=True,
        )


@pytest.mark.asyncio
async def test_join_memory_requires_audio(tmp_path: Path) -> None:
    with pytest.raises(cli.CliError, match="--memory"):
        await cli.join_meeting(
            "https://meet.google.com/abc-defg-hij",
            twin_name=None,
            owner_id=None,
            twin_id=None,
            meeting_id=None,
            lobby_timeout=None,
            duration=None,
            profile_dir=None,
            settings=fake_settings(tmp_path),
            memory=True,
        )


@pytest.mark.asyncio
async def test_join_wake_phrase_requires_converse(tmp_path: Path) -> None:
    with pytest.raises(cli.CliError, match="--wake-phrase"):
        await cli.join_meeting(
            "https://meet.google.com/abc-defg-hij",
            twin_name=None,
            owner_id=None,
            twin_id=None,
            meeting_id=None,
            lobby_timeout=None,
            duration=None,
            profile_dir=None,
            settings=fake_settings(tmp_path),
            wake_phrase="hey fakeyou",
        )


@pytest.mark.asyncio
async def test_join_barge_in_requires_converse(tmp_path: Path) -> None:
    with pytest.raises(cli.CliError, match="--barge-in"):
        await cli.join_meeting(
            "https://meet.google.com/abc-defg-hij",
            twin_name=None,
            owner_id=None,
            twin_id=None,
            meeting_id=None,
            lobby_timeout=None,
            duration=None,
            profile_dir=None,
            settings=fake_settings(tmp_path),
            barge_in=True,
        )


@pytest.mark.asyncio
async def test_join_wake_phrase_gates_agent_replies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_calls = []

    class FakeAgent:
        async def ainvoke(self, request, config=None):
            agent_calls.append(request)
            return {"messages": [AIMessage(content="Balasan.")]}

    monkeypatch.setattr(cli, "create_meeting_agent", lambda *args, **kwargs: FakeAgent())

    def capture_run(monkeypatch):
        async def run(config, **kwargs):
            loop = kwargs["conversation"]
            loop.bind(SimpleNamespace(speak=lambda text: asyncio.sleep(0)))
            for ident, text in (("t1", "status proyek"), ("t2", "hey fakeyou halo")):
                frame = SimpleNamespace(
                    id=ident,
                    text=text,
                    finalized=True,
                    user_id="",
                    timestamp="2026-09-28T00:00:00+00:00",
                )
                await kwargs["open_audio"].keywords["on_transcript"](frame)
            await loop.wait_idle()
            return SimpleNamespace(phase="ended", terminal_reason="owner_stop", detail="ok")

        monkeypatch.setattr(cli, "run_meeting_session", run)
        return {}

    monkeypatch.setattr(sys.modules[__name__], "_capture_run", capture_run)
    await _join_with_audio(
        tmp_path, monkeypatch, conversation_enabled=True, wake_phrase="hey fakeyou"
    )
    assert len(agent_calls) == 1


@pytest.mark.asyncio
async def test_join_barge_in_stops_speaking_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stops = []

    class FakeAgent:
        async def ainvoke(self, request, config=None):
            return {"messages": [AIMessage(content="Balasan.")]}

    async def stop_playback():
        stops.append(True)

    monkeypatch.setattr(cli, "create_meeting_agent", lambda *args, **kwargs: FakeAgent())

    def capture_run(monkeypatch):
        async def run(config, **kwargs):
            loop = kwargs["conversation"]
            loop.bind(
                SimpleNamespace(
                    speak=lambda text: asyncio.sleep(0),
                    is_speaking=True,
                    stop_playback=stop_playback,
                )
            )
            frame = SimpleNamespace(
                id="t1",
                text="tunggu",
                finalized=True,
                user_id="",
                timestamp="2026-09-28T00:00:00+00:00",
            )
            await kwargs["open_audio"].keywords["on_transcript"](frame)
            await loop.wait_idle()
            return SimpleNamespace(phase="ended", terminal_reason="owner_stop", detail="ok")

        monkeypatch.setattr(cli, "run_meeting_session", run)
        return {}

    monkeypatch.setattr(sys.modules[__name__], "_capture_run", capture_run)
    await _join_with_audio(tmp_path, monkeypatch, conversation_enabled=True, barge_in=True)
    assert stops == [True]


@pytest.mark.asyncio
async def test_join_playback_events_are_journaled_and_finished_speech_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    def capture_run(monkeypatch):
        async def run(config, **kwargs):
            handler = kwargs["open_audio"].keywords["on_playback_event"]
            handler(PlaybackEvent("speak_started", "halo", ""))
            handler(PlaybackEvent("speak_dropped", "terpotong", "interrupted"))
            handler(PlaybackEvent("speak_finished", "halo", ""))
            return SimpleNamespace(phase="ended", terminal_reason="owner_stop", detail="ok")

        monkeypatch.setattr(cli, "run_meeting_session", run)
        return {}

    monkeypatch.setattr(sys.modules[__name__], "_capture_run", capture_run)
    await _join_with_audio(tmp_path, monkeypatch)

    rows = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{"')
    ]
    playback = [
        (row["event"], row["text"], row["detail"])
        for row in rows
        if row.get("source") == "playback"
    ]
    assert playback == [
        ("speak_started", "halo", None),
        ("speak_dropped", "terpotong", "interrupted"),
        ("speak_finished", "halo", None),
    ]
    assert all(
        row["meeting_id"] == "meeting-9" and row["at"]
        for row in rows
        if row.get("source") == "playback"
    )
    entries = read_meeting_record(meeting_record_path(tmp_path / "meetings", "meeting-9"))
    assert [(entry.kind, entry.text) for entry in entries] == [("twin_speech", "halo")]


@pytest.mark.asyncio
async def test_join_audio_wires_journal_and_preflight_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = await _join_with_audio(tmp_path, monkeypatch)

    journal = captured["open_audio"].keywords["journal"]
    assert journal._meeting_id == "meeting-9"
    assert captured["preflight_hosts"] == ("api.openai.com", "api.meta.ai")
    assert captured["preflight_budget_seconds"] == 5.0


@pytest.mark.asyncio
async def test_join_conversation_receives_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "create_meeting_agent", lambda *args, **kwargs: object())
    captured = await _join_with_audio(tmp_path, monkeypatch, conversation_enabled=True)

    loop = captured["conversation"]
    assert loop._responder.journal is not None
    assert captured["preflight_hosts"] == ("api.openai.com", "api.meta.ai")


@pytest.mark.asyncio
async def test_join_without_audio_skips_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    async def fake_run(config, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(phase="ended", terminal_reason="owner_stop", detail="ok")

    monkeypatch.setattr(cli, "run_meeting_session", fake_run)
    await cli.join_meeting(
        "https://meet.google.com/abc-defg-hij",
        twin_name=None,
        owner_id=None,
        twin_id=None,
        meeting_id="meeting-1",
        lobby_timeout=None,
        duration=None,
        profile_dir=None,
        settings=fake_settings(tmp_path),
    )

    assert "preflight_hosts" not in captured


PERSONA_DATA = {
    "owner_id": "settings-owner",
    "twin_id": "settings-twin",
    "display_name": "Rani Twin",
    "role": "Product lead for the synthetic Orchid project",
    "verified_facts": ["The Orchid beta launch is planned for 14 October 2026"],
    "allowed_tools": ["recall_meeting_context"],
}
MEET_LINK = "https://meet.google.com/abc-defg-hij"


def write_persona(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "persona.json"
    path.write_text(json.dumps({**PERSONA_DATA, **overrides}), encoding="utf-8")
    return path


def persona_join_arguments(tmp_path: Path, settings: SimpleNamespace, **overrides) -> dict:
    arguments = {
        "twin_name": None,
        "owner_id": None,
        "twin_id": None,
        "meeting_id": "meeting-9",
        "lobby_timeout": None,
        "duration": None,
        "profile_dir": None,
        "settings": settings,
        "audio_enabled": True,
        "conversation_enabled": True,
    }
    arguments.update(overrides)
    return arguments


def forbid_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args, **_kwargs):
        pytest.fail("resources must not open before persona validation")

    monkeypatch.setattr(cli, "init_chat_model", fail)
    monkeypatch.setattr(cli, "run_meeting_session", fail)
    monkeypatch.setattr(memory_module, "create_graphiti_memory", fail)


class FakeGraphiti:
    def __init__(
        self,
        *,
        facts: tuple[str, ...] = (),
        connect_error: BaseException | None = None,
        connect_gate: asyncio.Event | None = None,
        search_error: BaseException | None = None,
        search_gate: asyncio.Event | None = None,
    ) -> None:
        self.facts = facts
        self.connect_error = connect_error
        self.connect_gate = connect_gate
        self.search_error = search_error
        self.search_gate = search_gate
        self.events: list[str] = []
        self.searched: list[dict] = []
        self.added: list[dict] = []
        self.search_started = asyncio.Event()
        self.search_cancelled = asyncio.Event()

    async def verify_connection(self) -> None:
        self.events.append("verify")
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        if self.connect_error is not None:
            raise self.connect_error

    async def search(self, query: str, **kwargs) -> list[SimpleNamespace]:
        self.searched.append({"query": query, **kwargs})
        self.search_started.set()
        if self.search_gate is not None:
            try:
                await self.search_gate.wait()
            except asyncio.CancelledError:
                self.search_cancelled.set()
                raise
        if self.search_error is not None:
            raise self.search_error
        return [SimpleNamespace(fact=fact, episodes=[]) for fact in self.facts]

    async def add_episode(self, **kwargs) -> SimpleNamespace:
        self.added.append(kwargs)
        return SimpleNamespace(
            episode=SimpleNamespace(uuid=f"episode-{len(self.added)}", name=kwargs["name"]),
            edges=[],
        )

    async def retrieve_episodes(self, reference_time, **kwargs) -> list[SimpleNamespace]:
        return []

    async def close(self) -> None:
        self.events.append("close")


class ToolCallingModel(FakeMessagesListChatModel):
    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


def recall_then_reply(reply: str) -> ToolCallingModel:
    return ToolCallingModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "recall_meeting_context",
                        "args": {"query": "Orchid launch"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content=reply),
        ]
    )


class RecordingAudio:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.on_playback_event = None

    @property
    def is_speaking(self) -> bool:
        return False

    async def speak(self, text: str) -> None:
        self.spoken.append(text)
        if self.on_playback_event is not None:
            self.on_playback_event(PlaybackEvent("speak_finished", text))

    async def stop_playback(self) -> None:
        return None


async def _join_persona_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: FakeMessagesListChatModel,
    graphiti: FakeGraphiti | None = None,
    transcripts: tuple[str, ...] = ("Kapan launch Orchid?",),
    settle=None,
    persona_overrides: dict | None = None,
    **overrides,
) -> tuple[dict, RecordingAudio, dict]:
    settings = fake_settings(tmp_path)
    settings.model_api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    monkeypatch.setattr(cli, "init_chat_model", lambda *_args, **_kwargs: model)

    @asynccontextmanager
    async def memory_saver(_value):
        yield InMemorySaver()

    monkeypatch.setattr(cli.AsyncSqliteSaver, "from_conn_string", memory_saver)
    if graphiti is not None:
        monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config: graphiti)
        monkeypatch.setattr(cli, "verify_memory_connection", FakeGraphiti.verify_connection)
    else:
        monkeypatch.setattr(
            memory_module,
            "create_graphiti_memory",
            lambda _config: pytest.fail("graphiti must stay closed without --memory"),
        )
    created: dict = {}

    def recording_agent(agent_model, **kwargs):
        created.update(kwargs)
        return create_meeting_agent(agent_model, **kwargs)

    monkeypatch.setattr(cli, "create_meeting_agent", recording_agent)
    audio = RecordingAudio()
    outcome: dict = {}

    async def run(config, **kwargs):
        loop = kwargs["conversation"]
        outcome["config"] = config
        outcome["conversation"] = loop
        audio.on_playback_event = kwargs["open_audio"].keywords["on_playback_event"]
        loop.bind(audio)
        for index, text in enumerate(transcripts):
            frame = SimpleNamespace(
                id=f"t{index}",
                text=text,
                finalized=True,
                user_id="",
                timestamp=f"2026-09-30T00:00:0{index}+00:00",
            )
            await kwargs["open_audio"].keywords["on_transcript"](frame)
        if settle is None:
            await loop.wait_idle()
        else:
            await settle()
        outcome["phase"] = loop.phase
        return SimpleNamespace(phase="ended", terminal_reason="owner_stop", detail="ok")

    monkeypatch.setattr(cli, "run_meeting_session", run)
    arguments = persona_join_arguments(
        tmp_path,
        settings,
        persona_path=write_persona(tmp_path, **(persona_overrides or {})),
        **overrides,
    )
    await cli.join_meeting(MEET_LINK, **arguments)
    return created, audio, outcome


async def test_join_persona_requires_converse(tmp_path: Path) -> None:
    with pytest.raises(cli.CliError, match="--persona requires --converse"):
        await cli.join_meeting(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                fake_settings(tmp_path),
                audio_enabled=False,
                conversation_enabled=False,
                persona_path=write_persona(tmp_path),
            ),
        )


async def test_join_memory_requires_persona(tmp_path: Path, monkeypatch) -> None:
    forbid_resources(monkeypatch)
    with pytest.raises(cli.CliError, match="--memory requires --persona"):
        await cli.join_meeting(
            MEET_LINK, **persona_join_arguments(tmp_path, fake_settings(tmp_path), memory=True)
        )


@pytest.mark.parametrize(
    "overrides", [{"owner_id": "other-owner"}, {"twin_id": "other-twin"}, {"unknown": "field"}]
)
async def test_join_rejects_invalid_persona_before_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict
) -> None:
    settings = fake_settings(tmp_path)
    settings.model_api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises(ValueError, match="owner_id and twin_id|Extra inputs"):
        await cli.join_meeting(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                memory=True,
                persona_path=write_persona(tmp_path, **overrides),
            ),
        )
    assert not settings.journal_dir.exists()


async def test_join_memory_rejects_persona_without_recall_permission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fake_settings(tmp_path)
    settings.model_api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises(cli.CliError, match="recall_meeting_context"):
        await cli.join_meeting(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                memory=True,
                persona_path=write_persona(tmp_path, allowed_tools=[]),
            ),
        )


async def test_join_rejects_unreadable_persona(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fake_settings(tmp_path)
    settings.model_api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises(cli.CliError, match="--persona cannot be read"):
        await cli.join_meeting(
            MEET_LINK,
            **persona_join_arguments(tmp_path, settings, persona_path=tmp_path / "missing.json"),
        )


@pytest.mark.parametrize("twin_name,expected", [(None, "Rani Twin"), ("Override", "Override")])
async def test_join_persona_display_name_yields_to_explicit_twin_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, twin_name: str | None, expected: str
) -> None:
    monkeypatch.setattr(cli, "create_meeting_agent", lambda *_args, **_kwargs: object())
    captured = await _join_with_audio(
        tmp_path,
        monkeypatch,
        conversation_enabled=True,
        twin_name=twin_name,
        persona_path=write_persona(tmp_path),
    )

    assert captured["config"].twin_name == expected
    assert (captured["config"].owner_id, captured["config"].twin_id) == (
        "settings-owner",
        "settings-twin",
    )


async def test_join_persona_without_memory_uses_profile_prompt_without_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, audio, _outcome = await _join_persona_conversation(
        tmp_path, monkeypatch, model=ToolCallingModel(responses=[AIMessage(content="Halo.")])
    )

    assert created["persona"] == build_persona(PersonaProfile.model_validate(PERSONA_DATA))
    assert created["tools"] == []
    assert created["middleware"] == []
    assert audio.spoken == ["Halo."]


async def test_join_persona_memory_exposes_only_profile_scoped_recall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = FakeGraphiti(facts=("Orchid beta launch is planned for 14 October 2026",))
    model = recall_then_reply("Launch Orchid dijadwalkan 14 Oktober.")

    created, audio, outcome = await _join_persona_conversation(
        tmp_path, monkeypatch, model=model, graphiti=graphiti, memory=True
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert [tool.name for tool in created["tools"]] == ["recall_meeting_context"]
    assert [type(item) for item in created["middleware"]] == [ToolErrorMiddleware]
    assert graphiti.searched == [
        {
            "query": "Orchid launch",
            "group_ids": [profile.memory_group],
            "num_results": memory_module.RECALL_SEARCH_LIMIT,
            "search_filter": memory_module.CURRENT_FACTS_FILTER,
        }
    ]
    assert profile.memory_group != fake_settings(tmp_path).memory_group
    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert [m.content for m in tool_results] == [
        f"{fact} (source: unknown)" for fact in graphiti.facts
    ]
    assert graphiti.added == []
    assert audio.spoken == ["Launch Orchid dijadwalkan 14 Oktober."]
    assert outcome["phase"] == "listening"
    assert graphiti.events == ["verify", "close"]


async def test_join_recall_failure_is_returned_as_unknown_without_failing_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graphiti = FakeGraphiti(search_error=RuntimeError("neo4j went away"))
    model = recall_then_reply("Aku belum tahu, nanti aku cek ke Rani.")

    _created, audio, outcome = await _join_persona_conversation(
        tmp_path, monkeypatch, model=model, graphiti=graphiti, memory=True
    )

    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert [m.content for m in tool_results] == [memory_module.RECALL_UNAVAILABLE_RESULT]
    assert audio.spoken == ["Aku belum tahu, nanti aku cek ke Rani."]
    assert outcome["phase"] == "listening"
    outcome["conversation"].raise_if_failed()
    faults = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith('{"event":"fault"')
    ]
    assert [(fault["source"], fault["kind"]) for fault in faults] == [("memory", "unknown")]
    assert "neo4j went away" not in json.dumps(faults)


async def test_join_stop_during_recall_cancels_without_late_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = FakeGraphiti(search_gate=asyncio.Event())
    model = recall_then_reply("Jawaban terlambat.")

    _created, audio, outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settle=graphiti.search_started.wait,
    )

    assert graphiti.search_cancelled.is_set()
    assert audio.spoken == []
    assert outcome["conversation"].phase == "closed"
    assert graphiti.events[-1] == "close"


async def test_join_persona_reply_keeps_two_sentence_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ToolCallingModel(
        responses=[AIMessage(content="Kalimat satu. Kalimat dua. Kalimat tiga.")]
    )

    _created, audio, _outcome = await _join_persona_conversation(tmp_path, monkeypatch, model=model)

    assert audio.spoken == ["Kalimat satu. Kalimat dua."]


@pytest.mark.parametrize(
    "graphiti_options,kind",
    [
        ({"connect_error": ConnectionRefusedError("refused")}, "unknown"),
        ({"connect_gate": asyncio.Event()}, "timeout"),
    ],
)
async def test_join_memory_startup_failure_exits_before_browser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    graphiti_options: dict,
    kind: str,
) -> None:
    graphiti = FakeGraphiti(**graphiti_options)
    saver_events: list[str] = []
    settings = fake_settings(tmp_path)
    settings.model_api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    monkeypatch.setattr(cli, "MEMORY_CONNECT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(cli, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config: graphiti)
    monkeypatch.setattr(cli, "verify_memory_connection", FakeGraphiti.verify_connection)
    monkeypatch.setattr(
        cli, "run_meeting_session", lambda *_a, **_k: pytest.fail("browser must stay closed")
    )
    monkeypatch.setattr(
        cli, "create_meeting_agent", lambda *_a, **_k: pytest.fail("agent must not be built")
    )

    @asynccontextmanager
    async def tracked_saver(_value):
        saver_events.append("open")
        try:
            yield InMemorySaver()
        finally:
            saver_events.append("close")

    monkeypatch.setattr(cli.AsyncSqliteSaver, "from_conn_string", tracked_saver)

    with pytest.raises(SystemExit) as caught:
        await cli.join_meeting(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path, settings, memory=True, persona_path=write_persona(tmp_path)
            ),
        )

    assert caught.value.code == 3
    assert graphiti.events == ["verify", "close"]
    assert saver_events == []
    captured = capsys.readouterr()
    assert '"terminal_reason": "infra_failure"' in captured.out
    assert "memory unavailable: memory" in captured.out
    assert f'"kind":"{kind}"' in captured.out
    assert '"exit_code": 3' in captured.err


def test_join_persona_flag_reaches_join(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_join(link, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli, "join_meeting", fake_join)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "join", MEET_LINK, "--audio", "--converse", "--persona", "p.json", "--memory"],
    )
    cli.main()

    assert captured["persona_path"] == Path("p.json")
    assert captured["memory"] is True


async def test_join_ingests_participant_transcripts_and_finished_twin_speech(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = FakeGraphiti()
    model = ToolCallingModel(responses=[AIMessage(content="Demo digeser ke Kamis ya.")])

    created, audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        transcripts=("Demo Orchid jadi kapan?",),
        persona_overrides={"ingest_transcripts": True},
    )

    profile = PersonaProfile.model_validate({**PERSONA_DATA, "ingest_transcripts": True})
    assert audio.spoken == ["Demo digeser ke Kamis ya."]
    [episode] = graphiti.added
    assert episode["episode_body"] == (
        "Unidentified participant: Demo Orchid jadi kapan?\nDigital twin: Demo digeser ke Kamis ya."
    )
    assert episode["name"] == "meeting-9:w00000"
    assert episode["group_id"] == profile.memory_group
    assert episode["saga"] == "meeting:meeting-9"
    assert [tool.name for tool in created["tools"]] == ["recall_meeting_context"]
    assert graphiti.events == ["verify", "close"]


async def test_join_ingest_only_persona_opens_memory_without_recall_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = FakeGraphiti()

    created, _audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingModel(responses=[AIMessage(content="Oke.")]),
        graphiti=graphiti,
        memory=True,
        persona_overrides={"ingest_transcripts": True, "allowed_tools": []},
    )

    assert created["tools"] == []
    assert created["middleware"] == []
    assert len(graphiti.added) == 1
    assert "recall" not in created["persona"].casefold()


async def test_join_without_ingest_permission_never_writes_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = FakeGraphiti()

    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingModel(responses=[AIMessage(content="Oke.")]),
        graphiti=graphiti,
        memory=True,
    )

    assert graphiti.added == []


def fake_persona_memory(monkeypatch: pytest.MonkeyPatch, memory: object) -> list[str]:
    opened: list[str] = []

    @asynccontextmanager
    async def open_memory(_settings):
        opened.append("open")
        yield memory
        opened.append("close")

    monkeypatch.setattr(cli, "open_persona_memory", open_memory)
    return opened


async def test_memory_add_stores_owner_note_in_persona_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graphiti = FakeGraphiti()
    opened = fake_persona_memory(monkeypatch, graphiti)

    await cli.add_owner_note(
        "Demo Orchid pindah ke Jumat.",
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        settings=fake_settings(tmp_path),
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    [note] = graphiti.added
    assert note["group_id"] == profile.memory_group
    assert note["source_description"] == memory_module.OWNER_NOTE_SOURCE
    assert opened == ["open", "close"]
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"episode_uuid": "episode-1", "facts": []}
    assert "no facts were extracted" in captured.err


@pytest.mark.parametrize(
    "text,identity,match",
    [
        ("  ", {}, "blank"),
        ("Catatan.", {"owner_id": "other-owner"}, "owner_id and twin_id"),
    ],
)
async def test_memory_add_validates_before_opening_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, identity: dict, match: str
) -> None:
    opened = fake_persona_memory(monkeypatch, FakeGraphiti())

    with pytest.raises(ValueError, match=match):
        await cli.add_owner_note(
            text,
            persona_path=write_persona(tmp_path),
            owner_id=identity.get("owner_id"),
            twin_id=None,
            settings=fake_settings(tmp_path),
        )
    assert opened == []


async def test_memory_forget_meeting_uses_persona_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graphiti = FakeGraphiti()
    fake_persona_memory(monkeypatch, graphiti)
    calls: list[dict] = []

    async def forget(memory, **kwargs):
        calls.append({"memory": memory, **kwargs})
        return 3

    monkeypatch.setattr(cli, "forget_meeting", forget)

    await cli.forget_memory(
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        meeting_id="meeting-9",
        episode_uuid=None,
        settings=fake_settings(tmp_path),
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert calls == [
        {"memory": graphiti, "group_id": profile.memory_group, "meeting_id": "meeting-9"}
    ]
    assert json.loads(capsys.readouterr().out) == {
        "removed_episodes": 3,
        "record_removed": False,
        "journal_files_removed": 0,
        "conversation_thread_removed": False,
    }


async def _forget_meeting_with_local_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, keep_record: bool
) -> tuple[SimpleNamespace, str]:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    settings = fake_settings(tmp_path)
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    record_path = meeting_record_path(settings.meeting_record_dir, "meeting-9")
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}\n")
    settings.journal_dir.mkdir()
    for name in ("meeting-9.jsonl", "meeting-9.jsonl.1", "meeting-10.jsonl"):
        (settings.journal_dir / name).write_text("{}\n")
    thread_id = conversation_thread_id(profile.owner_id, profile.twin_id, "meeting-9")
    other_thread = conversation_thread_id(profile.owner_id, profile.twin_id, "meeting-10")
    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
        agent = create_meeting_agent(
            FakeMessagesListChatModel(responses=[AIMessage(content="ok")]), checkpointer=saver
        )
        for thread in (thread_id, other_thread):
            await agent.ainvoke(
                {"messages": [{"role": "user", "content": "halo"}]},
                {"configurable": {"thread_id": thread}},
            )
    fake_persona_memory(monkeypatch, FakeGraphiti())

    async def forget(memory, **kwargs):
        return 2

    monkeypatch.setattr(cli, "forget_meeting", forget)
    await cli.forget_memory(
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        meeting_id="meeting-9",
        episode_uuid=None,
        settings=settings,
        keep_record=keep_record,
    )
    return settings, thread_id


async def _thread_exists(settings: SimpleNamespace, thread_id: str) -> bool:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
        return await saver.aget_tuple({"configurable": {"thread_id": thread_id}}) is not None


async def test_memory_forget_meeting_removes_record_journal_and_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings, thread_id = await _forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=False
    )

    assert json.loads(capsys.readouterr().out) == {
        "removed_episodes": 2,
        "record_removed": True,
        "journal_files_removed": 2,
        "conversation_thread_removed": True,
    }
    assert not (settings.meeting_record_dir / "meeting-9").exists()
    assert sorted(path.name for path in settings.journal_dir.iterdir()) == ["meeting-10.jsonl"]
    assert not await _thread_exists(settings, thread_id)
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    other = conversation_thread_id(profile.owner_id, profile.twin_id, "meeting-10")
    assert await _thread_exists(settings, other)


async def test_memory_forget_meeting_keep_record_only_removes_episodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings, thread_id = await _forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=True
    )

    assert json.loads(capsys.readouterr().out) == {"removed_episodes": 2}
    assert meeting_record_path(settings.meeting_record_dir, "meeting-9").exists()
    assert (settings.journal_dir / "meeting-9.jsonl").exists()
    assert await _thread_exists(settings, thread_id)


async def test_memory_forget_episode_uses_persona_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = FakeGraphiti()
    fake_persona_memory(monkeypatch, graphiti)
    calls: list[dict] = []

    async def forget(memory, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(cli, "forget_episode", forget)

    await cli.forget_memory(
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        meeting_id=None,
        episode_uuid="episode-7",
        settings=fake_settings(tmp_path),
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert calls == [{"group_id": profile.memory_group, "episode_uuid": "episode-7"}]


@pytest.mark.parametrize(
    "meeting_id,episode_uuid,match",
    [
        (None, None, "exactly one"),
        ("meeting-9", "episode-7", "exactly one"),
        ("bad meeting", None, "String should match pattern"),
    ],
)
async def test_memory_forget_validates_before_opening_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    meeting_id: str | None,
    episode_uuid: str | None,
    match: str,
) -> None:
    opened = fake_persona_memory(monkeypatch, FakeGraphiti())

    with pytest.raises(ValueError, match=match):
        await cli.forget_memory(
            persona_path=write_persona(tmp_path),
            owner_id=None,
            twin_id=None,
            meeting_id=meeting_id,
            episode_uuid=episode_uuid,
            settings=fake_settings(tmp_path),
        )
    assert opened == []


@pytest.mark.parametrize(
    "argv,target,expected",
    [
        (
            ["memory-add", "Catatan.", "--persona", "p.json", "--owner", "o", "--twin-id", "t"],
            "add_owner_note",
            {"persona_path": Path("p.json"), "owner_id": "o", "twin_id": "t"},
        ),
        (
            ["memory-forget", "--meeting", "m1", "--persona", "p.json"],
            "forget_memory",
            {"persona_path": Path("p.json"), "meeting_id": "m1", "episode_uuid": None},
        ),
    ],
)
def test_memory_operator_commands_parse_arguments(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], target: str, expected: dict
) -> None:
    captured: dict = {}

    async def fake_command(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli, target, fake_command)
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    cli.main()

    assert {key: captured[key] for key in expected} == expected


def test_memory_forget_rejects_both_targets_at_parse_time(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "memory-forget", "--meeting", "m1", "--episode", "e1", "--persona", "p"],
    )
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2


def test_main_reports_unavailable_memory_with_infra_exit_code(monkeypatch, capsys) -> None:
    from fakeyou.provider_faults import ProviderFault

    async def unavailable(*_args, **_kwargs):
        raise ProviderFault("memory", "unavailable")

    monkeypatch.setattr(cli, "add_owner_note", unavailable)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "memory-add", "x", "--persona", "p.json"])
    with pytest.raises(SystemExit) as caught:
        cli.main()

    assert caught.value.code == 3
    assert "memory unavailable" in capsys.readouterr().err


async def test_join_hung_recall_times_out_as_unknown_and_keeps_listening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graphiti = FakeGraphiti(search_gate=asyncio.Event())
    model = recall_then_reply("Aku belum tahu, nanti aku cek ke Rani.")
    monkeypatch.setattr(memory_module, "RECALL_TIMEOUT_SECONDS", 0.01)

    _created, audio, outcome = await _join_persona_conversation(
        tmp_path, monkeypatch, model=model, graphiti=graphiti, memory=True
    )

    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert [m.content for m in tool_results] == [memory_module.RECALL_UNAVAILABLE_RESULT]
    assert graphiti.search_cancelled.is_set()
    assert audio.spoken == ["Aku belum tahu, nanti aku cek ke Rani."]
    assert outcome["phase"] == "listening"
    assert '"source":"memory","kind":"timeout"' in capsys.readouterr().out


async def test_join_journals_session_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run(config, **kwargs):
        return SimpleNamespace(phase="ended", terminal_reason="infra_failure", detail="dns")

    monkeypatch.setattr(cli, "run_meeting_session", fake_run)

    with pytest.raises(SystemExit):
        await cli.join_meeting(
            MEET_LINK,
            twin_name=None,
            owner_id=None,
            twin_id=None,
            meeting_id="meeting-1",
            lobby_timeout=None,
            duration=None,
            profile_dir=None,
            settings=fake_settings(tmp_path),
        )

    rows = [
        json.loads(line)
        for line in (tmp_path / "journals" / "meeting-1.jsonl").read_text().splitlines()
    ]
    [result] = [row for row in rows if row["event"] == "session_result"]
    assert (result["terminal_reason"], result["exit_code"]) == ("infra_failure", 3)


def test_report_command_prints_summary_from_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    (journal_dir / "meeting-1.jsonl").write_text(
        json.dumps(
            {
                "event": "session_result",
                "meeting_id": "meeting-1",
                "at": "2026-09-30T10:00:00+00:00",
                "phase": "ended",
                "terminal_reason": "owner_stop",
                "exit_code": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "report", "--meeting", "meeting-1", "--journal-dir", str(journal_dir)],
    )

    cli.main()

    report = json.loads(capsys.readouterr().out)
    assert report["meeting_id"] == "meeting-1"
    assert report["result"]["terminal_reason"] == "owner_stop"


@pytest.mark.parametrize("meeting_id,match", [("missing", "no journal"), ("bad id", "pattern")])
def test_report_command_rejects_unknown_or_invalid_meeting(
    tmp_path: Path, meeting_id: str, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        cli.show_session_report(meeting_id, tmp_path, fake_settings(tmp_path))


def test_chat_model_uses_role_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        cli, "init_chat_model", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    settings = fake_settings(tmp_path)
    settings.chat_model = "openai:muse-spark-1.3"
    settings.chat_base_url = "https://api.meta.ai/v1"
    settings.chat_api_key = SecretStr("meta-key")
    settings.chat_reasoning_effort = "minimal"

    cli.create_chat_model(settings)

    assert calls == [
        (
            ("openai:muse-spark-1.3",),
            {
                "api_key": "meta-key",
                "timeout": 60,
                "max_retries": 2,
                "base_url": "https://api.meta.ai/v1",
                "reasoning_effort": "minimal",
            },
        )
    ]


def test_chat_model_defaults_to_openai_key_without_provider_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        cli, "init_chat_model", lambda *args, **kwargs: calls.append((args, kwargs))
    )

    cli.create_chat_model(fake_settings(tmp_path))

    assert calls == [(("fake:model",), {"api_key": "test-key", "timeout": 60, "max_retries": 2})]


@pytest.mark.parametrize(
    "chat_model,chat_base_url,expected",
    [
        ("openai:gpt-4.1-mini", None, ("api.openai.com", "api.meta.ai")),
        ("openai:muse-spark-1.3", "https://api.meta.ai/v1", ("api.openai.com", "api.meta.ai")),
        (
            "openai:local",
            "https://llm.example.com/v1",
            ("api.openai.com", "api.meta.ai", "llm.example.com"),
        ),
        ("anthropic:claude", None, ("api.openai.com", "api.meta.ai")),
    ],
)
def test_preflight_hosts_follow_the_configured_chat_endpoint(
    tmp_path: Path, chat_model: str, chat_base_url: str | None, expected: tuple
) -> None:
    settings = fake_settings(tmp_path)
    settings.chat_model = chat_model
    settings.chat_base_url = chat_base_url

    assert cli.preflight_hosts(settings) == expected


def test_check_reports_models_per_role(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = fake_settings(tmp_path)
    settings.model_api_key = SecretStr("muse-key")

    cli.check_setup(settings)

    models = json.loads(capsys.readouterr().out)["models"]
    assert models["reply"] == {"model": "fake:model", "base_url": None, "reasoning_effort": None}
    assert models["memory"] == {
        "llm": "memory-model",
        "small": "memory-small",
        "embedding": "memory-embedding",
    }
    assert models["stt"]["model"] == "muse-voice-transcribe-1.0"
    assert models["tts"]["model"]


def write_record(settings: SimpleNamespace, meeting_id: str) -> None:
    from fakeyou.conversation import TranscriptEvent
    from fakeyou.record import MeetingRecord

    record = MeetingRecord(meeting_record_path(settings.meeting_record_dir, meeting_id))
    record.add_transcript(
        TranscriptEvent(
            id="t1",
            timestamp="2026-09-30T10:00:00+00:00",
            source="meeting_audio",
            text="Kickoff vendor Orchid hari Selasa.",
            finalized=True,
        )
    )
    record.add_twin_speech("Siap, Selasa ya.")
    record.close()


async def test_memory_ingest_command_ingests_the_record_into_the_persona_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = fake_settings(tmp_path)
    write_record(settings, "meeting-9")
    graphiti = FakeGraphiti()
    fake_persona_memory(monkeypatch, graphiti)

    await cli.ingest_recorded_meeting(
        meeting_id="meeting-9",
        persona_path=write_persona(tmp_path, vocabulary=["Orchid"]),
        owner_id=None,
        twin_id=None,
        twin_name=None,
        settings=settings,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    [episode] = graphiti.added
    assert episode["group_id"] == profile.memory_group
    assert "Orchid" in episode["custom_extraction_instructions"]
    assert "'Rani Twin'" in episode["custom_extraction_instructions"]
    output = capsys.readouterr().out.splitlines()
    summary = json.loads(output[-1])
    assert (summary["meeting_id"], summary["ingested"], summary["completed"]) == (
        "meeting-9",
        1,
        True,
    )
    journal = (settings.journal_dir / "meeting-9.jsonl").read_text()
    assert '"event":"memory_ingest_finished"' in journal


async def test_memory_ingest_command_exits_1_when_ingestion_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = fake_settings(tmp_path)
    write_record(settings, "meeting-9")
    graphiti = FakeGraphiti()

    async def failing_add(**kwargs):
        raise RuntimeError("graph down")

    graphiti.add_episode = failing_add
    fake_persona_memory(monkeypatch, graphiti)

    with pytest.raises(SystemExit) as caught:
        await cli.ingest_recorded_meeting(
            meeting_id="meeting-9",
            persona_path=write_persona(tmp_path),
            owner_id=None,
            twin_id=None,
            twin_name=None,
            settings=settings,
        )
    assert caught.value.code == 1


async def test_memory_ingest_command_requires_a_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = fake_persona_memory(monkeypatch, FakeGraphiti())

    with pytest.raises(cli.CliError, match="no meeting record"):
        await cli.ingest_recorded_meeting(
            meeting_id="meeting-9",
            persona_path=write_persona(tmp_path),
            owner_id=None,
            twin_id=None,
            twin_name=None,
            settings=fake_settings(tmp_path),
        )
    assert opened == []


async def test_memory_forget_keep_record_requires_a_meeting(tmp_path: Path) -> None:
    with pytest.raises(cli.CliError, match="--keep-record"):
        await cli.forget_memory(
            persona_path=write_persona(tmp_path),
            owner_id=None,
            twin_id=None,
            meeting_id=None,
            episode_uuid="episode-7",
            settings=fake_settings(tmp_path),
            keep_record=True,
        )


async def test_join_passes_persona_vocabulary_as_speech_keywords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "create_meeting_agent", lambda *_args, **_kwargs: object())
    captured = await _join_with_audio(
        tmp_path,
        monkeypatch,
        conversation_enabled=True,
        persona_path=write_persona(tmp_path, vocabulary=["Orchid", "Budi"]),
    )

    assert captured["open_audio"].keywords["stt_keywords"] == ("Rani Twin", "Orchid", "Budi")


async def test_join_without_persona_has_no_speech_keywords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = await _join_with_audio(tmp_path, monkeypatch)

    assert captured["open_audio"].keywords["stt_keywords"] is None
