import asyncio
import signal
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr

import fakeyou.cli as cli
import fakeyou.memory as memory_module
from fakeyou.audio import (
    AGENT_MIC_SOURCE_NAME,
    AGENT_SINK_NAME,
    CAPTURE_SAMPLE_RATE,
    MEETING_MONITOR_SOURCE_NAME,
    MEETING_SINK_NAME,
    PLAYBACK_SAMPLE_RATE,
)
from fakeyou.conversation import ConversationLoop, conversation_thread_id
from fakeyou.playback import PlaybackEvent


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

    def fake_agent(model, *, tools=(), checkpointer=None, persona=""):
        created["tools"] = list(tools)
        created["checkpointer"] = checkpointer
        return SimpleNamespace(name="agent")

    monkeypatch.setattr(cli, "create_meeting_agent", fake_agent)
    captured = await _join_with_audio(tmp_path, monkeypatch, conversation_enabled=True)

    loop = captured["conversation"]
    assert isinstance(loop, ConversationLoop)
    assert loop.phase == "closed"
    assert created["tools"] == []
    assert created["checkpointer"] is not None


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
    assert '"event": "transcript_final"' in capsys.readouterr().out


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
        if '"event": "conversation_transition"' in line
    ]
    assert len(transitions) == 3
    assert all('"timestamp": "' in line for line in transitions)


@pytest.mark.asyncio
async def test_join_memory_wires_memory_tools_into_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: dict = {}

    def fake_agent(model, *, tools=(), checkpointer=None, persona=""):
        created["tools"] = list(tools)
        return SimpleNamespace(name="agent")

    @asynccontextmanager
    async def fake_memory(config):
        yield SimpleNamespace(name="memory")

    monkeypatch.setattr(cli, "create_meeting_agent", fake_agent)
    monkeypatch.setattr(cli, "open_graphiti_memory", fake_memory)
    monkeypatch.setattr(cli, "create_memory_tools", lambda memory, group_id: ["recall"])
    await _join_with_audio(tmp_path, monkeypatch, memory=True, conversation_enabled=True)

    assert created["tools"] == ["recall"]


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
async def test_join_playback_events_print_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    captured = await _join_with_audio(tmp_path, monkeypatch)

    handler = captured["open_audio"].keywords["on_playback_event"]
    handler(PlaybackEvent("speak_finished", "halo", ""))

    out = capsys.readouterr().out
    assert '"event": "speak_finished"' in out
    assert '"meeting_id": "meeting-9"' in out
    assert '"text": "halo"' in out


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
