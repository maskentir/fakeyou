import asyncio
import json
import signal
import sys
import threading
from contextlib import asynccontextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from langchain.agents.middleware import ToolErrorMiddleware
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

import fakeyou.assembly as assembly
import fakeyou.cli.chat as cli_chat
import fakeyou.cli.diagnostics as cli_diagnostics
import fakeyou.cli.errors as cli_errors
import fakeyou.cli.join as cli_join
import fakeyou.cli.main as cli_main
import fakeyou.cli.memory as cli_memory
import fakeyou.memory as memory_module
import fakeyou.providers as providers
from fakeyou import owner_notes
from fakeyou.agent import DEFAULT_PERSONA, create_meeting_agent
from fakeyou.brief import (
    MAX_BRIEF_BYTES,
    MeetingBrief,
    MeetingContext,
    brief_digest,
    context_path,
    read_meeting_context,
    write_meeting_context,
)
from fakeyou.conversation import ConversationLoop, conversation_thread_id
from fakeyou.greetings import create_greeting_middleware
from fakeyou.invariants import (
    AGENT_MIC_SOURCE_NAME,
    AGENT_SINK_NAME,
    CAPTURE_SAMPLE_RATE,
    MEETING_MONITOR_SOURCE_NAME,
    MEETING_SINK_NAME,
    PLAYBACK_SAMPLE_RATE,
)
from fakeyou.journal import journal_path
from fakeyou.owner_notes import NoteConflictError, OwnerNoteResult
from fakeyou.persona import PersonaProfile, build_system_prompt, persona_digest
from fakeyou.playback import PlaybackEvent
from fakeyou.record import load_meeting_record, meeting_record_path
from fakeyou.session import SessionResult
from fakeyou.settings import (
    DEFAULTS,
    CompatibleReply,
    EmbedderSettings,
    GoogleReply,
    MiniMaxTTS,
    Settings,
)
from fakeyou.voice import MiniMaxSpeechSynthesizer, create_speech_synthesizer
from tests.fakes import FakeReplyAudio, ToolCallingFakeModel, make_graphiti, recall_call


@pytest.mark.asyncio
async def test_chat_waits_for_memory_ready_before_exposing_tools(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    main_thread = threading.get_ident()
    memory = make_graphiti()
    memory.events = events

    def create_memory(_config, **_kwargs):
        events.append("construct")
        memory.factory_thread = threading.get_ident()
        return memory

    def create_tools(_memory, *, group_id, recall):
        events.append(f"tools:{group_id}")
        return []

    class FakeAgent:
        async def ainvoke(self, *_args, **_kwargs):
            events.append("invoke")
            return {"messages": [SimpleNamespace(content="ok")]}

    def create_agent(*_args, **_kwargs):
        events.append("agent")
        return FakeAgent()

    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(memory_module, "create_graphiti_memory", create_memory)
    monkeypatch.setattr(cli_chat, "create_memory_tools", create_tools)
    monkeypatch.setattr(cli_chat, "create_meeting_agent", create_agent)

    await cli_chat.chat("hello", "thread", False, True, settings)

    assert memory.factory_thread != main_thread
    assert events == ["construct", "tools:test-group", "agent", "invoke", "close"]


@pytest.mark.asyncio
async def test_memory_init_builds_indexes(
    settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[str] = []
    memory = make_graphiti()
    memory.events = events

    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config, **_kwargs: memory)

    await cli_memory.initialize_memory(settings)

    assert events == ["build", "close"]
    assert "Graphiti indexes initialized." in capsys.readouterr().out


@pytest.mark.asyncio
async def test_memory_init_closes_memory_when_index_setup_fails(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    memory = make_graphiti(fail_build=True)
    memory.events = events

    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config, **_kwargs: memory)

    with pytest.raises(RuntimeError, match="index setup failed"):
        await cli_memory.initialize_memory(settings)

    assert events == ["build", "close"]


@pytest.mark.asyncio
async def test_join_builds_session_config_and_prints_result(
    session_run,
    join,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured = session_run

    session_run.result.update(
        {"phase": "ended", "terminal_reason": "denied_by_host", "detail": "nope"}
    )

    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    await join(
        "https://meet.google.com/abc-defg-hij",
        twin_name="Twin",
        owner_id="owner-1",
        twin_id="twin-1",
        lobby_timeout=60.0,
        duration=600.0,
        profile_dir="/tmp/custom-profile",
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
    session_run,
    join,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured = session_run

    session_run.result.update(
        {"phase": "ended", "terminal_reason": "stopped_by_owner", "detail": "ok"}
    )

    await join(
        "https://meet.google.com/abc-defg-hij",
        meeting_id=None,
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
        ("stopped_by_owner", 0),
        ("denied_by_host", 0),
        ("ended_by_host", 0),
        ("duration_limit_reached", 0),
        ("alone_limit_reached", 0),
        ("browser_failure", 1),
        ("audio_failure", 1),
        ("conversation_failure", 1),
        ("infra_failure", 3),
        (None, 1),
    ],
)
async def test_join_exit_code_matches_terminal_reason(
    session_run,
    join,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    terminal_reason: str,
    exit_code: int,
) -> None:
    session_run.result.update(
        {"phase": "ended", "terminal_reason": terminal_reason, "detail": "done"}
    )

    with pytest.raises(SystemExit) if exit_code else nullcontext() as caught:
        await join("https://meet.google.com/abc-defg-hij")
    if exit_code:
        assert caught.value.code == exit_code

    captured = capsys.readouterr()
    rows = [
        json.loads(line)
        for line in (tmp_path / "journals" / "meeting-1.jsonl").read_text().splitlines()
    ]
    [exit_row] = [row for row in rows if row["event"] == "cli_exited"]
    assert exit_row["exit_code"] == exit_code
    assert exit_row["at"]
    assert exit_row["meeting_id"] == "meeting-1"
    expected = "null" if terminal_reason is None else json.dumps(terminal_reason)
    assert f'"terminal_reason": {expected}' in captured.out


async def test_join_result_carries_terminal_fault_report(
    session_run,
    join,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = {"faults": {"kinds": [{"kind": "timeout", "count": 2}], "total": 2}}

    session_run.result.update(
        {"phase": "ended", "terminal_reason": "stopped_by_owner", "detail": "ok", "faults": report}
    )

    await join(
        "https://meet.google.com/abc-defg-hij",
    )

    assert '"timeout"' in capsys.readouterr().out


async def test_join_without_audio_still_wires_faults_and_journal(
    session_run, join, tmp_path: Path
) -> None:
    from fakeyou.faults import FaultReporter
    from fakeyou.journal import Journal

    captured = session_run

    session_run.result.update(
        {"phase": "ended", "terminal_reason": "stopped_by_owner", "detail": "ok"}
    )

    await join(
        "https://meet.google.com/abc-defg-hij",
    )

    assert captured["config"].checkpoint_path == tmp_path / "checkpoints.sqlite"
    assert isinstance(captured["faults"], FaultReporter)
    assert isinstance(captured["journal"], Journal)
    assert "preflight_hosts" not in captured
    assert (tmp_path / "journals" / "meeting-1.jsonl").is_file()


@pytest.mark.parametrize("audio_enabled,speech", [(False, "hello"), (True, " ")])
async def test_join_rejects_invalid_speech_before_browser_start(join, audio_enabled, speech):
    with pytest.raises(ValueError, match="--speak"):
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            audio_enabled=audio_enabled,
            initial_speech=speech,
        )


def test_cli_error_is_value_error() -> None:
    assert issubclass(cli_errors.CliError, ValueError)


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
async def test_join_rejects_invalid_link(join, link) -> None:
    with pytest.raises(cli_errors.CliError, match="meet.google.com"):
        await join(
            link,
            meeting_id=None,
        )


@pytest.mark.parametrize(
    "lobby_timeout,duration",
    [(0.0, None), (-1.0, None), (float("nan"), None), (None, 0.0), (None, -5.0)],
)
async def test_join_rejects_nonpositive_timeouts(join, lobby_timeout, duration) -> None:
    with pytest.raises(cli_errors.CliError, match="positive"):
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            lobby_timeout=lobby_timeout,
            duration=duration,
        )


async def test_join_audio_precondition_raises_cli_error(join, monkeypatch) -> None:
    monkeypatch.setenv("PULSE_SOURCE", "wrong-source")
    with pytest.raises(cli_errors.CliError, match="--audio"):
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            audio_enabled=True,
        )


@pytest.mark.parametrize("thread", ["", "bad thread", "bad:thread", "x" * 129])
async def test_chat_rejects_invalid_thread(settings, monkeypatch, thread) -> None:
    monkeypatch.setattr(
        providers,
        "init_chat_model",
        lambda *_args, **_kwargs: pytest.fail("thread must be validated first"),
    )
    with pytest.raises(cli_errors.CliError, match="thread"):
        await cli_chat.chat("hello", thread, False, False, settings)


def test_main_exits_1_on_cli_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["fakeyou", "join", "not-a-link"])
    with pytest.raises(SystemExit) as excinfo:
        cli_main.main()
    assert excinfo.value.code == 1
    assert "meet.google.com" in capsys.readouterr().err


async def test_join_headless_override(session_run, join, settings) -> None:
    captured = session_run

    session_run.result.update(
        {"phase": "ended", "terminal_reason": "stopped_by_owner", "detail": "ok"}
    )

    await join(
        "https://meet.google.com/abc-defg-hij",
        meeting_id=None,
        headless=False,
    )
    assert captured["config"].headless is False
    settings.browser.headless = False
    await join(
        "https://meet.google.com/abc-defg-hij",
        meeting_id=None,
        headless=True,
    )
    assert captured["config"].headless is True
    await join(
        "https://meet.google.com/abc-defg-hij",
        meeting_id=None,
    )
    assert captured["config"].headless is False


@pytest.mark.parametrize(
    ("argv", "handler", "expected"),
    [
        (
            ["memory-reembed", "--persona", "p.json", "--owner-id", "o", "--twin-id", "t"],
            "reembed_memory",
            {"persona_path": Path("p.json"), "owner_id": "o", "twin_id": "t"},
        ),
        (
            ["memory-forget", "--meeting", "m1", "--persona", "p.json", "--keep-record"],
            "forget_memory",
            {"meeting_id": "m1", "episode_uuid": None, "keep_record": True},
        ),
        (
            ["report", "--meeting", "m1"],
            "show_session_report",
            {"meeting_id": "m1", "journal_dir": None},
        ),
        (
            ["chat", "hello", "--thread", "t1", "--memory"],
            "chat",
            {"message": "hello", "thread": "t1", "use_browser": False, "use_memory": True},
        ),
    ],
)
def test_main_dispatches_parsed_options_to_the_command_handler(
    monkeypatch, argv, handler, expected
) -> None:
    calls: list[dict] = []

    def record(**kwargs):
        calls.append(kwargs)

    async def record_async(**kwargs):
        calls.append(kwargs)

    sync_handlers = {"show_session_report"}
    monkeypatch.setattr(cli_main, handler, record if handler in sync_handlers else record_async)
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])

    cli_main.main()

    [call] = calls
    assert isinstance(call.pop("settings"), Settings)
    assert {key: call[key] for key in expected} == expected


def test_join_owner_id_flag(monkeypatch) -> None:
    captured: dict = {}

    async def fake_join(link, **kwargs):
        captured["link"] = link
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "join_meeting", fake_join)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "join", "https://meet.google.com/abc-defg-hij", "--owner-id", "owner-9"],
    )
    cli_main.main()
    assert captured["owner_id"] == "owner-9"


@pytest.mark.parametrize(
    "command", ["join", "memory-add", "memory-forget", "memory-ingest", "memory-reembed"]
)
def test_cli_rejects_owner_alias(command, capsys) -> None:
    arguments = {
        "join": ["https://meet.google.com/abc-defg-hij"],
        "memory-add": ["A note.", "--persona", "p.json"],
        "memory-forget": ["--meeting", "meeting-1", "--persona", "p.json"],
        "memory-ingest": ["--meeting", "meeting-1", "--persona", "p.json"],
        "memory-reembed": ["--persona", "p.json"],
    }
    with pytest.raises(SystemExit) as error:
        cli_main.build_parser().parse_args([command, *arguments[command], "--owner", "o"])
    assert error.value.code == 2
    assert "unrecognized arguments: --owner o" in capsys.readouterr().err


@pytest.mark.parametrize("argv,expected", [(["--headless"], True), ([], None)])
def test_join_headless_flag(monkeypatch, argv, expected) -> None:
    captured: dict = {}

    async def fake_join(link, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "join_meeting", fake_join)
    monkeypatch.setattr(
        sys, "argv", ["fakeyou", "join", "https://meet.google.com/abc-defg-hij", *argv]
    )
    cli_main.main()
    assert captured["headless"] is expected


def test_compose_pulse_defaults_match_audio_constants() -> None:
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text()
    assert f"PULSE_SOURCE: ${{PULSE_SOURCE:-{MEETING_MONITOR_SOURCE_NAME}}}" in compose
    assert f"PULSE_SINK: ${{PULSE_SINK:-{AGENT_SINK_NAME}}}" in compose


def test_compose_worker_reaches_neo4j_service_and_example_profiles() -> None:
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text()
    assert (
        "FAKEYOU_MEMORY__STORE__URI: ${FAKEYOU_COMPOSE_WORKER_NEO4J_URI:-bolt://neo4j:7687}"
        in compose
    )
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
    monkeypatch.setattr(cli_join, "find_spec", lambda name: SimpleNamespace())


async def _join_with_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, join, settings, session_run, **overrides
):
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    monkeypatch.setattr(providers, "init_chat_model", lambda *args, **kwargs: SimpleNamespace())

    @asynccontextmanager
    async def fake_saver(value):
        yield InMemorySaver()

    monkeypatch.setattr(assembly, "open_checkpointer", fake_saver)
    captured = session_run
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
    await join("https://meet.google.com/abc-defg-hij", **arguments)
    return captured


@pytest.mark.asyncio
async def test_join_audio_builds_conversation_with_meeting_thread(
    session_run, join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: dict = {}

    def fake_agent(model, *, tools=(), checkpointer=None, **kwargs):
        created["tools"] = list(tools)
        created["checkpointer"] = checkpointer
        created.update(kwargs)
        return SimpleNamespace(name="agent")

    monkeypatch.setattr(assembly, "create_meeting_agent", fake_agent)
    captured = await _join_with_audio(
        tmp_path,
        monkeypatch,
        conversation_enabled=True,
        settings=settings,
        join=join,
        session_run=session_run,
    )

    loop = captured["conversation"]
    assert isinstance(loop, ConversationLoop)
    assert loop.phase == "closed"
    assert created["tools"] == []
    assert created["checkpointer"] is not None
    assert created["persona"] == DEFAULT_PERSONA
    assert [type(item) for item in created["middleware"]] == [
        type(create_greeting_middleware("id"))
    ]
    assert loop._responder.journal is not None


@pytest.mark.asyncio
async def test_join_audio_transcript_reaches_conversation(
    session_run,
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    agent_calls: list = []

    class FakeAgent:
        async def ainvoke(self, request, config=None):
            agent_calls.append(config)
            return {"messages": [AIMessage(content="Reply.")]}

    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *args, **kwargs: FakeAgent())
    captured = {}

    async def run(config, **kwargs):
        captured.update(kwargs)
        loop = kwargs["conversation"]
        loop.bind(SimpleNamespace(speak=lambda text: asyncio.sleep(0)))
        frame = SimpleNamespace(
            id="t1",
            text="Hello.",
            finalized=True,
            user_id="",
            timestamp="2026-09-28T00:00:00+00:00",
        )
        await kwargs["open_audio"].keywords["on_transcript"](frame)
        await loop.wait_idle()
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    session_run.effect = run

    await _join_with_audio(
        tmp_path,
        monkeypatch,
        conversation_enabled=True,
        settings=settings,
        join=join,
        session_run=session_run,
    )

    assert [event.text for event in captured["conversation"].events] == ["Hello."]
    assert agent_calls[0]["configurable"]["thread_id"] == conversation_thread_id(
        "settings-owner", "settings-twin", "meeting-9"
    )
    rows = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{"')
    ]
    transitions = [row for row in rows if row["event"] == "conversation_transitioned"]
    assert len(transitions) == 3
    assert all(row["at"] for row in transitions)
    [final] = [row for row in rows if row["event"] == "transcript_finalized"]
    assert "text" not in final
    assert (final["participant_id"], final["meeting_id"]) == ("unknown", "meeting-9")
    assert final["speaker_evidence_id"] is None
    assert final["at"]
    entries = load_meeting_record(meeting_record_path(tmp_path / "meetings", "meeting-9"))
    assert [(entry.kind, entry.text) for entry in entries] == [("transcript", "Hello.")]


async def test_audio_only_does_not_construct_responding_agent(
    session_run, join, settings, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        assembly,
        "create_meeting_agent",
        lambda *args, **kwargs: pytest.fail("agent must be opt-in"),
    )
    captured = await _join_with_audio(
        tmp_path, monkeypatch, settings=settings, join=join, session_run=session_run
    )
    assert captured["conversation"] is None


async def test_conversation_option_requires_audio(join):
    with pytest.raises(cli_errors.CliError, match="--converse requires --audio"):
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            conversation_enabled=True,
        )


@pytest.mark.asyncio
async def test_join_memory_requires_audio(join) -> None:
    with pytest.raises(cli_errors.CliError, match="--memory"):
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            memory=True,
        )


@pytest.mark.asyncio
async def test_join_wake_phrase_requires_converse(join) -> None:
    with pytest.raises(cli_errors.CliError, match="--wake-phrase"):
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            wake_phrase="hey fakeyou",
        )


@pytest.mark.asyncio
async def test_join_barge_in_requires_converse(join) -> None:
    with pytest.raises(cli_errors.CliError, match="--barge-in"):
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            barge_in=True,
        )


@pytest.mark.asyncio
async def test_join_wake_phrase_gates_agent_replies(
    session_run, join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_calls = []

    class FakeAgent:
        async def ainvoke(self, request, config=None):
            agent_calls.append(request)
            return {"messages": [AIMessage(content="Reply.")]}

    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *args, **kwargs: FakeAgent())

    async def run(config, **kwargs):
        loop = kwargs["conversation"]
        loop.bind(SimpleNamespace(speak=lambda text: asyncio.sleep(0)))
        for ident, text in (("t1", "project status"), ("t2", "hey fakeyou hello")):
            frame = SimpleNamespace(
                id=ident,
                text=text,
                finalized=True,
                user_id="",
                timestamp="2026-09-28T00:00:00+00:00",
            )
            await kwargs["open_audio"].keywords["on_transcript"](frame)
        await loop.wait_idle()
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    session_run.effect = run

    await _join_with_audio(
        tmp_path,
        monkeypatch,
        conversation_enabled=True,
        wake_phrase="hey fakeyou",
        settings=settings,
        join=join,
        session_run=session_run,
    )
    assert len(agent_calls) == 1


@pytest.mark.asyncio
async def test_join_barge_in_stops_speaking_session(
    session_run, join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stops = []

    class FakeAgent:
        async def ainvoke(self, request, config=None):
            return {"messages": [AIMessage(content="Reply.")]}

    async def stop_playback():
        stops.append(True)

    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *args, **kwargs: FakeAgent())

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
            text="wait",
            finalized=True,
            user_id="",
            timestamp="2026-09-28T00:00:00+00:00",
        )
        await kwargs["open_audio"].keywords["on_transcript"](frame)
        await loop.wait_idle()
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    session_run.effect = run

    await _join_with_audio(
        tmp_path,
        monkeypatch,
        conversation_enabled=True,
        barge_in=True,
        settings=settings,
        join=join,
        session_run=session_run,
    )
    assert stops == [True]


@pytest.mark.asyncio
async def test_join_playback_events_are_journaled_and_finished_speech_recorded(
    session_run,
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    async def run(config, **kwargs):
        handler = kwargs["open_audio"].keywords["on_playback_event"]
        handler(PlaybackEvent("playback_started", "hello", ""))
        handler(PlaybackEvent("playback_dropped", "cut off", "interrupted"))
        handler(PlaybackEvent("playback_finished", "hello?  ", ""))
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    session_run.effect = run

    await _join_with_audio(
        tmp_path, monkeypatch, settings=settings, join=join, session_run=session_run
    )

    rows = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{"')
    ]
    playback = [
        (row["event"], row["ends_with_question"], row["detail"])
        for row in rows
        if row.get("source") == "playback"
    ]
    assert playback == [
        ("playback_started", False, None),
        ("playback_dropped", False, "interrupted"),
        ("playback_finished", True, None),
    ]
    assert all(
        row["meeting_id"] == "meeting-9" and row["at"]
        for row in rows
        if row.get("source") == "playback"
    )
    entries = load_meeting_record(meeting_record_path(tmp_path / "meetings", "meeting-9"))
    assert [(entry.kind, entry.text) for entry in entries] == [("twin_speech", "hello?  ")]


@pytest.mark.asyncio
async def test_join_audio_wires_journal_and_preflight_hosts(
    session_run, join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = await _join_with_audio(
        tmp_path, monkeypatch, settings=settings, join=join, session_run=session_run
    )

    journal = captured["open_audio"].keywords["journal"]
    assert journal._meeting_id == "meeting-9"
    assert captured["preflight_hosts"] == BASE_HOSTS
    assert captured["preflight_budget_seconds"] == 5.0


@pytest.mark.asyncio
async def test_join_without_audio_skips_preflight(
    session_run,
    join,
) -> None:
    captured = session_run

    session_run.result.update(
        {"phase": "ended", "terminal_reason": "stopped_by_owner", "detail": "ok"}
    )

    await join(
        "https://meet.google.com/abc-defg-hij",
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
BASE_HOSTS = ("https://api.openai.com/v1", "wss://api.meta.ai/v1/asr/realtime")


def record_chat_model_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    calls: list[tuple] = []
    monkeypatch.setattr(
        providers, "init_chat_model", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    return calls


def write_persona(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "persona.json"
    path.write_text(json.dumps({**PERSONA_DATA, **overrides}), encoding="utf-8")
    return path


def persona_join_arguments(tmp_path: Path, settings: Settings, **overrides) -> dict:
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

    monkeypatch.setattr(providers, "init_chat_model", fail)
    monkeypatch.setattr(cli_join, "run_meeting_session", fail)
    monkeypatch.setattr(memory_module, "create_graphiti_memory", fail)


def recall_then_reply(reply: str) -> ToolCallingFakeModel:
    return ToolCallingFakeModel(
        responses=[
            recall_call({"query": "Orchid launch"}, id="call-1"),
            AIMessage(content=reply),
        ]
    )


BRIEF_FACT = "The synthetic Orchid demo is booked for Friday."
BRIEF_DATA = {
    "schema_version": "meeting_brief/v1",
    "owner_id": "settings-owner",
    "twin_id": "settings-twin",
    "meeting_id": "meeting-9",
    "agenda": ["Synthetic Orchid kickoff agenda item"],
    "objectives": [{"id": "blockers", "text": "Find the synthetic kickoff blocker."}],
    "approved_facts": [{"id": "demo-date", "text": BRIEF_FACT}],
    "questions": [{"id": "vendor-needs", "text": "Which synthetic vendor input is missing?"}],
    "permissions": ["explain_facts", "ask_brief_questions"],
    "knowledge_scope": "brief_only",
}
BRIEF_TEXTS = (
    "Synthetic Orchid kickoff agenda item",
    "Find the synthetic kickoff blocker.",
    BRIEF_FACT,
    "Which synthetic vendor input is missing?",
)
PRIVATE_MARKER = "PRIVATE-BRIEF-MARKER-5c1e"


def write_brief(tmp_path: Path, name: str = "brief.json", **overrides: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps({**BRIEF_DATA, **overrides}), encoding="utf-8")
    return path


def meeting_brief(**overrides: object) -> MeetingBrief:
    return MeetingBrief.model_validate_json(json.dumps({**BRIEF_DATA, **overrides}))


def expected_context(brief: MeetingBrief, profile: PersonaProfile) -> MeetingContext:
    return MeetingContext(
        schema_version="meeting_context/v1",
        brief=brief,
        brief_sha256=brief_digest(brief),
        persona_sha256=persona_digest(profile),
    )


def meeting_context_file(settings: Settings, meeting_id: str = "meeting-9") -> Path:
    return context_path(meeting_record_path(settings.paths.record_dir, meeting_id))


def meeting_files(settings: Settings) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for root in (settings.paths.record_dir, settings.paths.journal_dir)
        if root.exists()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def journal_rows(settings: Settings, meeting_id: str = "meeting-9") -> list[dict]:
    path = journal_path(settings.paths.journal_dir, meeting_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def prepare_brief_join(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)


async def seed_conversation_thread(settings: Settings, meeting_id: str) -> str:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    thread_id = conversation_thread_id("settings-owner", "settings-twin", meeting_id)
    async with AsyncSqliteSaver.from_conn_string(str(settings.paths.checkpoint_path)) as saver:
        agent = create_meeting_agent(
            FakeMessagesListChatModel(responses=[AIMessage(content="ok")]), checkpointer=saver
        )
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": "hello"}]},
            {"configurable": {"thread_id": thread_id}},
        )
    return thread_id


async def _join_persona_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    join,
    settings,
    model: FakeMessagesListChatModel,
    graphiti: MagicMock | None = None,
    transcripts: tuple[str, ...] = ("When does Orchid launch?",),
    settle=None,
    persona_overrides: dict | None = None,
    audio: FakeReplyAudio | None = None,
    on_start=None,
    native_checkpointer: bool = False,
    **overrides,
) -> tuple[dict, FakeReplyAudio, dict]:
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: model)

    @asynccontextmanager
    async def memory_saver(_value):
        yield InMemorySaver()

    if not native_checkpointer:
        monkeypatch.setattr(assembly, "open_checkpointer", memory_saver)
    if graphiti is not None:
        monkeypatch.setattr(
            memory_module, "create_graphiti_memory", lambda _config, **_kwargs: graphiti
        )
    else:
        monkeypatch.setattr(
            memory_module,
            "create_graphiti_memory",
            lambda _config, **_kwargs: pytest.fail("graphiti must stay closed without --memory"),
        )
    created: dict = {}

    def recording_agent(agent_model, **kwargs):
        created.update(kwargs)
        return create_meeting_agent(agent_model, **kwargs)

    monkeypatch.setattr(assembly, "create_meeting_agent", recording_agent)
    audio = audio if audio is not None else FakeReplyAudio()
    outcome: dict = {}

    async def run(config, **kwargs):
        if on_start is not None:
            on_start()
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
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    monkeypatch.setattr(cli_join, "run_meeting_session", run)
    arguments = persona_join_arguments(
        tmp_path,
        settings,
        persona_path=write_persona(tmp_path, **(persona_overrides or {})),
        **overrides,
    )
    await join(MEET_LINK, **arguments)
    return created, audio, outcome


async def test_join_persona_requires_converse(join, settings, tmp_path: Path) -> None:
    with pytest.raises(cli_errors.CliError, match="--persona requires --converse"):
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                audio_enabled=False,
                conversation_enabled=False,
                persona_path=write_persona(tmp_path),
            ),
        )


async def test_join_memory_requires_persona(join, settings, tmp_path: Path, monkeypatch) -> None:
    forbid_resources(monkeypatch)
    with pytest.raises(cli_errors.CliError, match="--memory requires --persona"):
        await join(MEET_LINK, **persona_join_arguments(tmp_path, settings, memory=True))


@pytest.mark.parametrize(
    "overrides", [{"owner_id": "other-owner"}, {"twin_id": "other-twin"}, {"unknown": "field"}]
)
async def test_join_rejects_invalid_persona_before_resources(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict
) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises(ValueError, match="owner_id and twin_id|Extra inputs"):
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                memory=True,
                persona_path=write_persona(tmp_path, **overrides),
            ),
        )
    assert not settings.paths.journal_dir.exists()


async def test_join_memory_rejects_persona_without_recall_permission(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises(cli_errors.CliError, match="recall_meeting_context"):
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                memory=True,
                persona_path=write_persona(tmp_path, allowed_tools=[]),
            ),
        )


async def test_join_rejects_unreadable_persona(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    forbid_resources(monkeypatch)

    with pytest.raises(cli_errors.CliError, match="--persona cannot be read"):
        await join(
            MEET_LINK,
            **persona_join_arguments(tmp_path, settings, persona_path=tmp_path / "missing.json"),
        )


@pytest.mark.parametrize("twin_name,expected", [(None, "Rani Twin"), ("Override", "Override")])
async def test_join_persona_display_name_yields_to_explicit_twin_name(
    session_run,
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    twin_name: str | None,
    expected: str,
) -> None:
    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *_args, **_kwargs: object())
    captured = await _join_with_audio(
        tmp_path,
        monkeypatch,
        conversation_enabled=True,
        twin_name=twin_name,
        persona_path=write_persona(tmp_path),
        settings=settings,
        join=join,
        session_run=session_run,
    )

    assert captured["config"].twin_name == expected
    assert (captured["config"].owner_id, captured["config"].twin_id) == (
        "settings-owner",
        "settings-twin",
    )


async def test_join_persona_without_memory_uses_profile_prompt_without_tools(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Hello.")]),
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert created["persona"] == build_system_prompt(
        profile.model_copy(update={"allowed_tools": frozenset()})
    )
    assert "recall_meeting_context" not in created["persona"]
    assert created["tools"] == []
    assert [type(item) for item in created["middleware"]] == [
        type(create_greeting_middleware("id"))
    ]
    assert audio.spoken == ["Hello."]


async def test_join_persona_memory_exposes_only_profile_scoped_recall(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.memory.recall.strategy = "tool"
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    fact = "Orchid beta launch is planned for 14 October 2026"
    graphiti = make_graphiti(facts=(fact,), episodes={fact: ["meet-1"]})
    model = recall_then_reply("Orchid launches on 14 October.")
    episode = SimpleNamespace(
        uuid="meet-1",
        group_id=profile.memory_group,
        source_description="Google Meet meeting-9 transcript",
        valid_at=datetime(2026, 9, 30, 9, tzinfo=UTC),
        created_at=datetime(2026, 9, 30, 9, tzinfo=UTC),
    )

    async def get_by_uuids(driver, uuids):
        assert driver is graphiti.driver
        return [episode for uuid in uuids if uuid == "meet-1"]

    monkeypatch.setattr(memory_module.EpisodicNode, "get_by_uuids", get_by_uuids)

    created, audio, outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    assert [tool.name for tool in created["tools"]] == ["recall_meeting_context"]
    greeting_type = type(create_greeting_middleware("id"))
    assert [type(item) for item in created["middleware"]] == [
        greeting_type,
        ToolErrorMiddleware,
    ]
    assert graphiti.searched == [
        {
            "query": "Orchid launch",
            "group_ids": [profile.memory_group],
            "num_results": DEFAULTS.memory.recall.search_limit,
            "search_filter": memory_module.CURRENT_FACTS_FILTER,
        }
    ]
    assert profile.memory_group != settings.memory.store.group
    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    [content] = [m.content for m in tool_results]
    payload = json.loads(content)
    assert payload["schema"] == "memory_evidence/v1"
    assert [item["text"] for item in payload["facts"]] == [fact]
    assert payload["facts"][0]["sources"][0]["episode_id"] == "meet-1"
    assert graphiti.added == []
    assert audio.spoken == ["Orchid launches on 14 October."]
    assert outcome["phase"] == "listening"
    assert graphiti.events == ["verify", "close"]


async def test_join_recall_failure_is_returned_as_unknown_without_failing_conversation(
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.memory.recall.strategy = "tool"
    graphiti = make_graphiti(search_error=RuntimeError("neo4j went away"))
    model = recall_then_reply("I don't know yet, I'll check with Rani.")

    _created, audio, outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert [m.content for m in tool_results] == [memory_module.RECALL_UNAVAILABLE_RESULT]
    assert audio.spoken == ["I don't know yet, I'll check with Rani."]
    assert outcome["phase"] == "listening"
    outcome["conversation"].raise_if_failed()
    faults = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith('{"event":"fault_recorded"')
    ]
    assert [(fault["source"], fault["kind"]) for fault in faults] == [("memory", "unknown")]
    assert "neo4j went away" not in json.dumps(faults)


async def test_join_stop_during_recall_cancels_without_late_reply(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = make_graphiti(search_gate=asyncio.Event())
    model = recall_then_reply("A late answer.")

    _created, audio, outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settle=graphiti.search_started.wait,
        settings=settings,
        join=join,
    )

    assert graphiti.search_cancelled.is_set()
    assert audio.spoken == []
    assert outcome["conversation"].phase == "closed"
    assert graphiti.events[-1] == "close"


async def test_join_persona_reply_keeps_two_sentence_cap(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = ToolCallingFakeModel(
        responses=[AIMessage(content="Sentence one. Sentence two. Sentence three.")]
    )

    _created, audio, _outcome = await _join_persona_conversation(
        tmp_path, monkeypatch, model=model, settings=settings, join=join
    )

    assert audio.spoken == ["Sentence one. Sentence two."]


@pytest.mark.parametrize(
    "graphiti_options,kind",
    [
        ({"connect_error": ConnectionRefusedError("refused")}, "unknown"),
        ({"connect_gate": asyncio.Event()}, "timeout"),
    ],
)
async def test_join_memory_startup_failure_exits_before_browser(
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    graphiti_options: dict,
    kind: str,
) -> None:
    graphiti = make_graphiti(**graphiti_options)
    saver_events: list[str] = []
    settings.stt.api_key = SecretStr("muse-key")
    _enable_audio_platform(monkeypatch)
    monkeypatch.setattr(providers, "MEMORY_CONNECT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        memory_module, "create_graphiti_memory", lambda _config, **_kwargs: graphiti
    )
    monkeypatch.setattr(
        cli_join, "run_meeting_session", lambda *_a, **_k: pytest.fail("browser must stay closed")
    )
    monkeypatch.setattr(
        assembly, "create_meeting_agent", lambda *_a, **_k: pytest.fail("agent must not be built")
    )

    @asynccontextmanager
    async def tracked_saver(_value):
        saver_events.append("open")
        try:
            yield InMemorySaver()
        finally:
            saver_events.append("close")

    monkeypatch.setattr(assembly, "open_checkpointer", tracked_saver)

    with pytest.raises(SystemExit) as caught:
        await join(
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
    assert '"exit_code":3' in captured.out


def test_join_persona_flag_reaches_join(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_join(link, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "join_meeting", fake_join)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "join", MEET_LINK, "--audio", "--converse", "--persona", "p.json", "--memory"],
    )
    cli_main.main()

    assert captured["persona_path"] == Path("p.json")
    assert captured["memory"] is True
    assert captured["brief_path"] is None


def test_join_brief_flag_reaches_join(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    async def fake_join(link, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "join_meeting", fake_join)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fakeyou",
            "join",
            MEET_LINK,
            "--audio",
            "--converse",
            "--persona",
            "p.json",
            "--meeting-id",
            "meeting-9",
            "--brief",
            "b.json",
        ],
    )
    cli_main.main()

    assert captured["brief_path"] == Path("b.json")
    assert captured["meeting_id"] == "meeting-9"


async def test_join_defers_ingest_then_memory_ingest_writes_episode(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.memory.recall.strategy = "tool"
    from fakeyou.ingestion_status import read_status, status_path

    graphiti = make_graphiti()
    model = ToolCallingFakeModel(responses=[AIMessage(content="The demo moves to Thursday.")])

    created, audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        transcripts=("When is the Orchid demo now?",),
        persona_overrides={"ingest_transcripts": True},
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate({**PERSONA_DATA, "ingest_transcripts": True})
    assert audio.spoken == ["The demo moves to Thursday."]
    assert graphiti.added == []
    assert graphiti.events == ["verify", "close"]
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.status == "pending"
    assert status.record_sha256 is not None

    fake_persona_memory(monkeypatch, graphiti)
    await cli_memory.ingest_recorded_meeting(
        meeting_id="meeting-9",
        persona_path=write_persona(tmp_path, ingest_transcripts=True),
        owner_id=None,
        twin_id=None,
        twin_name=None,
        settings=settings,
    )

    [episode] = graphiti.added
    assert episode["episode_body"] == (
        "Unidentified participant: When is the Orchid demo now?\n"
        "Digital twin: The demo moves to Thursday."
    )
    assert episode["name"] == "meeting-9:w00000"
    assert episode["group_id"] == profile.memory_group
    assert episode["saga"] == "meeting:meeting-9"
    assert [tool.name for tool in created["tools"]] == ["recall_meeting_context"]


async def test_join_ingest_only_persona_defers_memory_without_recall_tool(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.ingestion_status import read_status, status_path

    graphiti = make_graphiti()

    created, _audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Okay.")]),
        graphiti=graphiti,
        memory=True,
        persona_overrides={"ingest_transcripts": True, "allowed_tools": []},
        settings=settings,
        join=join,
    )

    assert created["tools"] == []
    assert [type(item) for item in created["middleware"]] == [
        type(create_greeting_middleware("id"))
    ]
    assert graphiti.added == []
    assert "recall" not in created["persona"].casefold()
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.status == "pending"

    fake_persona_memory(monkeypatch, graphiti)
    await cli_memory.ingest_recorded_meeting(
        meeting_id="meeting-9",
        persona_path=write_persona(tmp_path, ingest_transcripts=True, allowed_tools=[]),
        owner_id=None,
        twin_id=None,
        twin_name=None,
        settings=settings,
    )

    assert len(graphiti.added) == 1


async def test_join_without_ingest_permission_never_writes_memory(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = make_graphiti()

    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Okay.")]),
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    assert graphiti.added == []


@pytest.mark.parametrize(
    "overrides,match",
    [
        (
            {"audio_enabled": False, "conversation_enabled": False, "persona_path": None},
            "--brief requires --converse",
        ),
        ({"persona_path": None}, "--brief requires --persona"),
        ({"meeting_id": None}, "--brief requires an explicit --meeting-id"),
        ({"meeting_id": ""}, "--brief requires an explicit --meeting-id"),
    ],
)
async def test_join_brief_requires_converse_persona_and_explicit_meeting_id(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict, match: str
) -> None:
    forbid_resources(monkeypatch)
    arguments = persona_join_arguments(
        tmp_path, settings, persona_path=write_persona(tmp_path), brief_path=write_brief(tmp_path)
    )
    arguments.update(overrides)

    with pytest.raises(cli_errors.CliError, match=match):
        await join(MEET_LINK, **arguments)
    assert not settings.paths.journal_dir.exists()
    assert not settings.paths.record_dir.exists()


async def test_join_brief_only_refuses_memory_before_resources(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(cli_errors.CliError, match="--memory.*brief_only"):
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                memory=True,
                persona_path=write_persona(tmp_path),
                brief_path=write_brief(tmp_path),
            ),
        )
    assert not settings.paths.journal_dir.exists()
    assert not settings.paths.record_dir.exists()


@pytest.mark.parametrize(
    "content,match",
    [
        (None, "--brief cannot be read"),
        ("oversize", "--brief is invalid: meeting brief must be at most"),
        ({"owner_id": "other-owner"}, "--brief is invalid: .*must match the session"),
        ({"twin_id": "other-twin"}, "--brief is invalid: .*must match the session"),
        ({"meeting_id": "meeting-10"}, "--brief is invalid: .*must match the session"),
        (
            {"approved_facts": [{"id": "secret", "text": PRIVATE_MARKER * 40}]},
            "--brief is invalid",
        ),
        ({"permissions": [f"approve_{PRIVATE_MARKER}"]}, "--brief is invalid"),
        (
            {PRIVATE_MARKER: "unknown key"},
            r"--brief is invalid: 1 validation error\(s\): extra_forbidden$",
        ),
    ],
)
async def test_join_rejects_an_unusable_brief_before_resources(
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: dict | str | None,
    match: str,
) -> None:
    prepare_brief_join(settings, monkeypatch)
    if content is None:
        brief_path = tmp_path / "missing-brief.json"
    elif content == "oversize":
        brief_path = tmp_path / "oversize-brief.json"
        brief_path.write_text(
            json.dumps(BRIEF_DATA) + " " * MAX_BRIEF_BYTES + PRIVATE_MARKER, encoding="utf-8"
        )
    else:
        assert isinstance(content, dict)
        brief_path = write_brief(tmp_path, **content)

    with pytest.raises(cli_errors.CliError, match=match) as caught:
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path, settings, persona_path=write_persona(tmp_path), brief_path=brief_path
            ),
        )
    assert PRIVATE_MARKER not in str(caught.value)
    assert all(text not in str(caught.value) for text in BRIEF_TEXTS)
    assert not settings.paths.journal_dir.exists()
    assert not settings.paths.record_dir.exists()


async def test_join_brief_only_snapshots_context_before_the_agent_runs(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    context_file = meeting_context_file(settings)
    observed: list[bool] = []

    class SnapshotObservingModel(ToolCallingFakeModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            observed.append(context_file.exists())
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    model = SnapshotObservingModel(responses=[AIMessage(content="Hello.")])
    created, audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        brief_path=write_brief(tmp_path),
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    brief = meeting_brief()
    assert observed == [True]
    assert created["tools"] == []
    assert [type(item) for item in created["middleware"]] == [
        type(create_greeting_middleware("id"))
    ]
    assert created["persona"] == build_system_prompt(profile, brief=brief)
    assert model.seen[0][0].content == created["persona"]
    assert audio.spoken == ["Hello."]
    assert read_meeting_context(context_file) == expected_context(brief, profile)
    assert not settings.paths.checkpoint_path.exists()
    printed = printed_join_summary(capsys)
    assert printed["brief_sha256"] == brief_digest(brief)
    assert printed["context_path"] == str(context_file)
    assert printed["session_outcome"] == "completed"


async def test_join_persona_memory_brief_exposes_only_profile_scoped_recall(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.memory.recall.strategy = "tool"
    graphiti = make_graphiti()

    created, audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Okay.")]),
        graphiti=graphiti,
        memory=True,
        brief_path=write_brief(tmp_path, knowledge_scope="persona_memory"),
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    brief = meeting_brief(knowledge_scope="persona_memory")
    assert [tool.name for tool in created["tools"]] == ["recall_meeting_context"]
    assert created["persona"] == build_system_prompt(profile, brief=brief)
    assert "recall_meeting_context" in created["persona"]
    assert audio.spoken == ["Okay."]
    assert graphiti.events == ["verify", "close"]
    assert read_meeting_context(meeting_context_file(settings)) == expected_context(brief, profile)


async def test_join_persona_memory_brief_without_memory_omits_recall_rules(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created, _audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Okay.")]),
        brief_path=write_brief(tmp_path, knowledge_scope="persona_memory"),
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    brief = meeting_brief(knowledge_scope="persona_memory")
    assert created["tools"] == []
    assert created["persona"] == build_system_prompt(
        profile.model_copy(update={"allowed_tools": frozenset()}), brief=brief
    )
    assert "recall_meeting_context" not in created["persona"]
    assert read_meeting_context(meeting_context_file(settings)) == expected_context(brief, profile)


@pytest.mark.parametrize("reformatted", [False, True])
async def test_join_brief_resume_reuses_the_matching_context_unchanged(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reformatted: bool
) -> None:
    brief_path = write_brief(tmp_path)
    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Hello.")]),
        brief_path=brief_path,
        settings=settings,
        join=join,
    )
    context_file = meeting_context_file(settings)
    recorded = context_file.read_bytes()
    if reformatted:
        reordered = dict(reversed(list(BRIEF_DATA.items())))
        reordered["permissions"] = list(reversed(BRIEF_DATA["permissions"]))
        brief_path.write_text(json.dumps(reordered, indent=4), encoding="utf-8")

    _created, audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Again.")]),
        brief_path=brief_path,
        settings=settings,
        join=join,
    )

    assert audio.spoken == ["Again."]
    assert context_file.read_bytes() == recorded
    reused = [row["reused"] for row in journal_rows(settings) if row["event"] == "brief_recorded"]
    assert reused == [False, True]


@pytest.mark.parametrize(
    "change,match",
    [
        ("persona", "meeting context differs.*new meeting ID"),
        ("brief", "meeting context differs.*new meeting ID"),
        ("without_brief", "meeting-9 was started with --brief"),
    ],
)
async def test_join_rejects_a_changed_context_without_touching_meeting_files(
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
    match: str,
) -> None:
    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Hello.")]),
        brief_path=write_brief(tmp_path),
        persona_overrides={"ingest_transcripts": True},
        settings=settings,
        join=join,
    )
    before = meeting_files(settings)
    forbid_resources(monkeypatch)
    arguments = persona_join_arguments(
        tmp_path,
        settings,
        persona_path=write_persona(tmp_path, ingest_transcripts=True),
        brief_path=write_brief(tmp_path),
    )
    if change == "persona":
        arguments["persona_path"] = write_persona(
            tmp_path, ingest_transcripts=True, speaking_style="Formal and slow"
        )
    elif change == "brief":
        arguments["brief_path"] = write_brief(tmp_path, "other-brief.json", questions=[])
    else:
        arguments["brief_path"] = None

    with pytest.raises(cli_errors.CliError, match=match):
        await join(MEET_LINK, **arguments)
    assert meeting_files(settings) == before


async def test_join_keeps_the_snapshot_when_the_source_brief_changes(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief_path = write_brief(tmp_path)
    edited = [{"id": "demo-date", "text": "EDITED-FACT-MARKER"}]
    model = ToolCallingFakeModel(responses=[AIMessage(content="Hello.")])

    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        brief_path=brief_path,
        on_start=lambda: write_brief(tmp_path, approved_facts=edited),
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    original = meeting_brief()
    assert "EDITED-FACT-MARKER" in brief_path.read_text(encoding="utf-8")
    assert model.seen[0][0].content == build_system_prompt(profile, brief=original)
    assert "EDITED-FACT-MARKER" not in model.seen[0][0].content
    assert read_meeting_context(meeting_context_file(settings)) == expected_context(
        original, profile
    )
    forbid_resources(monkeypatch)
    with pytest.raises(cli_errors.CliError, match="meeting context differs"):
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path, settings, persona_path=write_persona(tmp_path), brief_path=brief_path
            ),
        )


async def test_join_brief_refuses_a_record_with_conversation_and_no_context(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_ingest_windows(settings, "meeting-9", 1)
    before = meeting_files(settings)
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(cli_errors.CliError, match="conversation data.*new meeting ID"):
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path),
            ),
        )
    assert meeting_files(settings) == before
    assert not meeting_context_file(settings).exists()


async def test_join_brief_refuses_a_checkpoint_thread_without_context(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    thread_id = await seed_conversation_thread(settings, "meeting-9")
    other_thread = await seed_conversation_thread(settings, "meeting-10")
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(cli_errors.CliError, match="conversation data.*new meeting ID"):
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path),
            ),
        )
    assert await _thread_exists(settings, thread_id)
    assert await _thread_exists(settings, other_thread)
    assert meeting_files(settings) == {}
    assert not settings.paths.journal_dir.exists()


@pytest.mark.parametrize("with_brief", [True, False])
async def test_join_refuses_a_corrupt_meeting_context_and_keeps_it(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_brief: bool
) -> None:
    context_file = meeting_context_file(settings)
    context_file.parent.mkdir(parents=True)
    context_file.write_text("{corrupt", encoding="utf-8")
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(cli_errors.CliError, match="meeting context .*corrupt"):
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path) if with_brief else None,
            ),
        )
    assert meeting_files(settings) == {"meeting-9/meeting-context.json": b"{corrupt"}
    assert not settings.paths.journal_dir.exists()


async def test_join_brief_refuses_an_unreadable_checkpoint_before_resources(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    garbage = b"not a sqlite database" * 64
    settings.paths.checkpoint_path.write_bytes(garbage)
    prepare_brief_join(settings, monkeypatch)

    with pytest.raises(
        cli_errors.CliError, match="conversation checkpoint cannot be checked: DatabaseError"
    ):
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path),
            ),
        )
    assert settings.paths.checkpoint_path.read_bytes() == garbage
    assert meeting_files(settings) == {}
    assert not settings.paths.journal_dir.exists()


async def test_join_brief_snapshot_write_failure_starts_nothing(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    prepare_brief_join(settings, monkeypatch)

    def reject_replace(source, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", reject_replace)

    with pytest.raises(cli_errors.CliError, match="meeting context not written"):
        await join(
            MEET_LINK,
            **persona_join_arguments(
                tmp_path,
                settings,
                persona_path=write_persona(tmp_path, ingest_transcripts=True),
                brief_path=write_brief(tmp_path),
            ),
        )
    assert meeting_files(settings) == {}
    assert not settings.paths.journal_dir.exists()


async def test_join_journals_brief_metadata_without_brief_text(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Hello.")]),
        brief_path=write_brief(tmp_path),
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    brief = meeting_brief()
    rows = journal_rows(settings)
    [row] = [row for row in rows if row["event"] == "brief_recorded"]
    assert {key: value for key, value in row.items() if key != "at"} == {
        "event": "brief_recorded",
        "meeting_id": "meeting-9",
        "source": "cli",
        "brief_sha256": brief_digest(brief),
        "persona_sha256": persona_digest(profile),
        "knowledge_scope": "brief_only",
        "permissions": ["ask_brief_questions", "explain_facts"],
        "agenda_count": 1,
        "objective_count": 1,
        "approved_fact_count": 1,
        "question_count": 1,
        "objective_ids": ["blockers"],
        "question_ids": ["vendor-needs"],
        "reused": False,
    }
    journal_text = (settings.paths.journal_dir / "meeting-9.jsonl").read_text(encoding="utf-8")
    printed = capsys.readouterr().out
    for text in BRIEF_TEXTS:
        assert text not in journal_text
        assert text not in printed


async def test_join_briefs_stay_isolated_per_meeting_and_resume_in_one_checkpointer(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    second_fact = "The synthetic vendor budget stays fixed."
    second = {
        "meeting_id": "meeting-10",
        "approved_facts": [{"id": "vendor-budget", "text": second_fact}],
    }
    first_model = ToolCallingFakeModel(responses=[AIMessage(content="First.")])
    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=first_model,
        brief_path=write_brief(tmp_path),
        transcripts=("First meeting question?",),
        native_checkpointer=True,
        settings=settings,
        join=join,
    )
    second_model = ToolCallingFakeModel(responses=[AIMessage(content="Second.")])
    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=second_model,
        meeting_id="meeting-10",
        brief_path=write_brief(tmp_path, "second-brief.json", **second),
        transcripts=("Second meeting question?",),
        native_checkpointer=True,
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    second_prompt = second_model.seen[0][0].content
    assert second_prompt == build_system_prompt(profile, brief=meeting_brief(**second))
    assert BRIEF_FACT not in second_prompt
    assert second_fact not in first_model.seen[0][0].content
    second_history = " ".join(str(message.content) for message in second_model.seen[0])
    assert "First meeting question?" not in second_history
    assert read_meeting_context(meeting_context_file(settings)) == expected_context(
        meeting_brief(), profile
    )
    assert read_meeting_context(meeting_context_file(settings, "meeting-10")) == (
        expected_context(meeting_brief(**second), profile)
    )

    retry_model = ToolCallingFakeModel(responses=[AIMessage(content="Retry.")])
    _created, audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=retry_model,
        brief_path=write_brief(tmp_path),
        transcripts=("Retry question?",),
        native_checkpointer=True,
        settings=settings,
        join=join,
    )

    assert audio.spoken == ["Retry."]
    retry_history = " ".join(str(message.content) for message in retry_model.seen[0])
    assert "First meeting question?" in retry_history
    assert "Second meeting question?" not in retry_history
    assert retry_model.seen[0][0].content == build_system_prompt(profile, brief=meeting_brief())


async def test_join_brief_stop_before_pcm_cancels_the_model_turn(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    class HangingModel(ToolCallingFakeModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            started.set()
            await asyncio.Event().wait()
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    _created, audio, outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=HangingModel(responses=[AIMessage(content="A late answer.")]),
        brief_path=write_brief(tmp_path),
        settle=started.wait,
        settings=settings,
        join=join,
    )

    assert audio.spoken == []
    assert outcome["conversation"].phase == "closed"
    rows = journal_rows(settings)
    assert [row["reason"] for row in rows if row["event"] == "agent_cancelled"] == ["close"]
    assert not [row for row in rows if row["event"] == "playback_finished"]
    entries = load_meeting_record(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    assert [entry.kind for entry in entries] == ["transcript"]
    assert read_meeting_context(meeting_context_file(settings)) is not None


async def test_join_brief_stop_during_playback_records_no_twin_speech(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BlockingAudio(FakeReplyAudio):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()

        async def speak(self, text, *, sentence_index=None, transcript_id=None):
            self.speaking = True
            self.on_playback_event(PlaybackEvent("playback_started", text))
            self.entered.set()
            await asyncio.Event().wait()

    blocking = BlockingAudio()
    _created, audio, outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="A long answer.")]),
        brief_path=write_brief(tmp_path),
        audio=blocking,
        settle=blocking.entered.wait,
        settings=settings,
        join=join,
    )

    assert audio is blocking
    assert audio.spoken == []
    assert outcome["conversation"].phase == "closed"
    playback = [row["event"] for row in journal_rows(settings) if row["source"] == "playback"]
    assert playback == ["playback_started"]
    entries = load_meeting_record(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    assert [entry.kind for entry in entries] == ["transcript"]
    assert read_meeting_context(meeting_context_file(settings)) is not None


@pytest.mark.parametrize(
    "profile_data,with_graphiti,match",
    [
        (None, False, "requires a persona profile"),
        (PERSONA_DATA, True, "brief_only"),
    ],
)
async def test_open_conversation_rejects_an_unusable_brief_before_resources(
    settings,
    monkeypatch: pytest.MonkeyPatch,
    profile_data: dict | None,
    with_graphiti: bool,
    match: str,
) -> None:
    monkeypatch.setattr(
        providers, "init_chat_model", lambda *_a, **_k: pytest.fail("model must stay closed")
    )
    monkeypatch.setattr(
        assembly, "open_checkpointer", lambda *_a, **_k: pytest.fail("saver must stay closed")
    )
    config = SimpleNamespace(owner_id="settings-owner", twin_id="settings-twin", meeting_id="m")

    with pytest.raises(ValueError, match=match):
        async with assembly.open_conversation(
            settings,
            config,
            profile=PersonaProfile.model_validate(profile_data) if profile_data else None,
            graphiti=make_graphiti() if with_graphiti else None,
            brief=meeting_brief(),
            on_transition=lambda *_args: None,
        ):
            pytest.fail("conversation must not open")


def fake_persona_memory(monkeypatch: pytest.MonkeyPatch, memory: object) -> list[str]:
    opened: list[str] = []

    @asynccontextmanager
    async def open_memory(_settings):
        opened.append("open")
        yield memory
        opened.append("close")

    for module in (cli_join, cli_memory):
        monkeypatch.setattr(module, "open_persona_memory", open_memory)
    return opened


async def test_memory_add_stores_owner_note_in_persona_group(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graphiti = make_graphiti()
    opened = fake_persona_memory(monkeypatch, graphiti)
    calls: list[dict] = []
    source_at = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    recorded_at = datetime(2026, 10, 2, 8, tzinfo=UTC)
    note_id = UUID("b322d326-37b5-482a-904c-ef982f4dc510")

    async def remember(memory, **kwargs):
        calls.append({"memory": memory, **kwargs})
        return OwnerNoteResult(
            episode_uuid=str(note_id),
            facts=("The Orchid demo moves to Friday.",),
            source_at=source_at,
            recorded_at=recorded_at,
            supersedes=None,
        )

    assert cli_memory.remember_owner_note is owner_notes.remember_owner_note
    monkeypatch.setattr(cli_memory, "remember_owner_note", remember)

    await cli_memory.add_owner_note(
        "The Orchid demo moves to Friday.",
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        settings=settings,
        source_at=source_at,
        note_id=note_id,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert calls == [
        {
            "memory": graphiti,
            "group_id": profile.memory_group,
            "text": "The Orchid demo moves to Friday.",
            "source_at": source_at,
            "note_id": note_id,
        }
    ]
    assert opened == ["open", "close"]
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "episode_uuid": str(note_id),
        "facts": ["The Orchid demo moves to Friday."],
        "source_at": source_at.isoformat(),
        "recorded_at": recorded_at.isoformat(),
        "supersedes": None,
    }
    assert captured.err == ""


async def test_memory_add_generates_and_reports_note_id_before_extraction(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    seen: list[dict] = []

    async def remember(memory, **kwargs):
        seen.append(kwargs)
        return OwnerNoteResult(
            episode_uuid=str(kwargs["note_id"]),
            facts=("A note.",),
            source_at=datetime(2026, 9, 30, 6, 40, tzinfo=UTC),
            recorded_at=datetime(2026, 10, 2, 8, tzinfo=UTC),
            supersedes=None,
        )

    monkeypatch.setattr(cli_memory, "remember_owner_note", remember)

    await cli_memory.add_owner_note(
        "A note.",
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        settings=settings,
    )

    [call] = seen
    assert UUID(str(call["note_id"])) == call["note_id"]
    assert f"note-id {call['note_id']}" in capsys.readouterr().err


async def test_memory_add_rejects_future_source_before_opening_memory(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(ValueError, match="future"):
        await cli_memory.add_owner_note(
            "A note.",
            persona_path=write_persona(tmp_path),
            owner_id=None,
            twin_id=None,
            settings=settings,
            source_at=datetime(2999, 1, 1, tzinfo=UTC),
        )
    assert opened == []


async def test_memory_add_maps_note_conflict_to_cli_error(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_persona_memory(monkeypatch, make_graphiti())

    async def conflicting(_memory, **_kwargs):
        raise NoteConflictError("note id already in use with different payload")

    monkeypatch.setattr(cli_memory, "remember_owner_note", conflicting)

    with pytest.raises(cli_errors.CliError, match="already in use"):
        await cli_memory.add_owner_note(
            "A note.",
            persona_path=write_persona(tmp_path),
            owner_id=None,
            twin_id=None,
            settings=settings,
        )


async def test_memory_correct_supersedes_target_note(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graphiti = make_graphiti()
    opened = fake_persona_memory(monkeypatch, graphiti)
    calls: list[dict] = []
    target = UUID("a1b2c3d4-0000-4000-8000-000000000001")
    note_id = UUID("a1b2c3d4-0000-4000-8000-000000000002")
    source_at = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    recorded_at = datetime(2026, 10, 2, 8, tzinfo=UTC)

    async def remember(memory, **kwargs):
        calls.append({"memory": memory, **kwargs})
        return OwnerNoteResult(
            episode_uuid=str(note_id),
            facts=("The Orchid demo moves to Friday.",),
            source_at=source_at,
            recorded_at=recorded_at,
            supersedes=str(target),
        )

    monkeypatch.setattr(cli_memory, "remember_owner_note", remember)

    await cli_memory.correct_owner_note(
        "The Orchid demo moves to Friday.",
        episode_uuid=str(target),
        source_at=source_at,
        note_id=note_id,
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        settings=settings,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert calls == [
        {
            "memory": graphiti,
            "group_id": profile.memory_group,
            "text": "The Orchid demo moves to Friday.",
            "source_at": source_at,
            "note_id": note_id,
            "supersedes": target,
        }
    ]
    assert opened == ["open", "close"]
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "episode_uuid": str(note_id),
        "facts": ["The Orchid demo moves to Friday."],
        "source_at": source_at.isoformat(),
        "recorded_at": recorded_at.isoformat(),
        "supersedes": str(target),
    }
    assert captured.err == ""


async def test_memory_correct_generates_note_id_when_missing(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_persona_memory(monkeypatch, make_graphiti())
    seen: list[dict] = []
    target = UUID("a1b2c3d4-0000-4000-8000-000000000001")
    source_at = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)

    async def remember(memory, **kwargs):
        seen.append(kwargs)
        return OwnerNoteResult(
            episode_uuid=str(kwargs["note_id"]),
            facts=("Fixed.",),
            source_at=source_at,
            recorded_at=datetime(2026, 10, 2, 8, tzinfo=UTC),
            supersedes=str(target),
        )

    monkeypatch.setattr(cli_memory, "remember_owner_note", remember)

    await cli_memory.correct_owner_note(
        "Fixed.",
        episode_uuid=str(target),
        source_at=source_at,
        note_id=None,
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        settings=settings,
    )

    [call] = seen
    assert call["supersedes"] == target
    assert f"note-id {call['note_id']}" in capsys.readouterr().err


_CORRECT_TARGET = "a1b2c3d4-0000-4000-8000-000000000001"
_CORRECT_SOURCE = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)


@pytest.mark.parametrize(
    "text,episode_uuid,source_at,identity,match",
    [
        ("  ", _CORRECT_TARGET, _CORRECT_SOURCE, {}, "blank"),
        ("Fixed.", "not-a-uuid", _CORRECT_SOURCE, {}, "UUID"),
        ("Fixed.", _CORRECT_TARGET, datetime(2999, 1, 1, tzinfo=UTC), {}, "future"),
        ("Fixed.", _CORRECT_TARGET, datetime(2026, 9, 30, 6, 40), {}, "timezone"),
        (
            "Fixed.",
            _CORRECT_TARGET,
            _CORRECT_SOURCE,
            {"owner_id": "other-owner"},
            "owner_id and twin_id",
        ),
    ],
)
async def test_memory_correct_validates_before_opening_memory(
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    episode_uuid: str,
    source_at: datetime,
    identity: dict,
    match: str,
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(ValueError, match=match):
        await cli_memory.correct_owner_note(
            text,
            episode_uuid=episode_uuid,
            source_at=source_at,
            note_id=None,
            persona_path=write_persona(tmp_path),
            owner_id=identity.get("owner_id"),
            twin_id=None,
            settings=settings,
        )
    assert opened == []


@pytest.mark.parametrize(
    "text,identity,match",
    [
        ("  ", {}, "blank"),
        ("A note.", {"owner_id": "other-owner"}, "owner_id and twin_id"),
    ],
)
async def test_memory_add_validates_before_opening_memory(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, identity: dict, match: str
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(ValueError, match=match):
        await cli_memory.add_owner_note(
            text,
            persona_path=write_persona(tmp_path),
            owner_id=identity.get("owner_id"),
            twin_id=None,
            settings=settings,
        )
    assert opened == []


async def test_memory_forget_meeting_uses_persona_group(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    calls: list[dict] = []

    async def forget(memory, **kwargs):
        calls.append({"memory": memory, **kwargs})
        return 3

    monkeypatch.setattr(cli_memory, "forget_meeting", forget)

    await cli_memory.forget_memory(
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        meeting_id="meeting-9",
        episode_uuid=None,
        settings=settings,
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, settings, keep_record: bool
) -> tuple[SimpleNamespace, str]:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}\n")
    for artifact in ("debrief.json", "debrief.md", "debrief-attempt.json"):
        record_path.with_name(artifact).write_text("synthetic artifact")
    write_meeting_context(context_path(record_path), expected_context(meeting_brief(), profile))
    settings.paths.journal_dir.mkdir()
    for name in ("meeting-9.jsonl", "meeting-9.jsonl.1", "meeting-10.jsonl"):
        (settings.paths.journal_dir / name).write_text("{}\n")
    thread_id = await seed_conversation_thread(settings, "meeting-9")
    await seed_conversation_thread(settings, "meeting-10")
    from fakeyou.lifecycle import MeetingLifecycle

    async with AsyncSqliteSaver.from_conn_string(str(settings.paths.checkpoint_path)) as saver:
        for meeting in ("meeting-9", "meeting-10"):
            lifecycle = MeetingLifecycle(profile.owner_id, profile.twin_id, meeting, saver)
            await lifecycle.initialize()
    fake_persona_memory(monkeypatch, make_graphiti())

    async def forget(memory, **kwargs):
        return 2

    monkeypatch.setattr(cli_memory, "forget_meeting", forget)
    await cli_memory.forget_memory(
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        meeting_id="meeting-9",
        episode_uuid=None,
        settings=settings,
        keep_record=keep_record,
    )
    return settings, thread_id


async def _thread_exists(settings: Settings, thread_id: str) -> bool:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(settings.paths.checkpoint_path)) as saver:
        return await saver.aget_tuple({"configurable": {"thread_id": thread_id}}) is not None


async def test_memory_forget_meeting_removes_record_journal_and_thread(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings, thread_id = await _forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=False, settings=settings
    )

    assert json.loads(capsys.readouterr().out) == {
        "removed_episodes": 2,
        "record_removed": True,
        "journal_files_removed": 2,
        "conversation_thread_removed": True,
    }
    assert not (settings.paths.record_dir / "meeting-9").exists()
    assert sorted(path.name for path in settings.paths.journal_dir.iterdir()) == [
        "meeting-10.jsonl"
    ]
    assert not await _thread_exists(settings, thread_id)
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    other = conversation_thread_id(profile.owner_id, profile.twin_id, "meeting-10")
    assert await _thread_exists(settings, other)


async def test_memory_forget_meeting_keep_record_only_removes_episodes(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings, thread_id = await _forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=True, settings=settings
    )

    assert json.loads(capsys.readouterr().out) == {"removed_episodes": 2}
    assert meeting_record_path(settings.paths.record_dir, "meeting-9").exists()
    assert (settings.paths.journal_dir / "meeting-9.jsonl").exists()
    assert await _thread_exists(settings, thread_id)
    for artifact in ("debrief.json", "debrief.md", "debrief-attempt.json"):
        assert (
            meeting_record_path(settings.paths.record_dir, "meeting-9").with_name(artifact).exists()
        )


@pytest.mark.parametrize("keep_record", [False, True])
async def test_memory_forget_keeps_the_meeting_context_only_with_the_record(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep_record: bool
) -> None:
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    context_file = meeting_context_file(settings)

    await _forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=keep_record, settings=settings
    )

    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    assert record_path.exists() is keep_record
    assert context_file.exists() is keep_record
    if keep_record:
        assert read_meeting_context(context_file) == expected_context(meeting_brief(), profile)


async def test_memory_forget_episode_uses_persona_group(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    calls: list[dict] = []

    async def forget(memory, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(cli_memory, "forget_episode", forget)

    await cli_memory.forget_memory(
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        meeting_id=None,
        episode_uuid="episode-7",
        settings=settings,
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
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    meeting_id: str | None,
    episode_uuid: str | None,
    match: str,
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(ValueError, match=match):
        await cli_memory.forget_memory(
            persona_path=write_persona(tmp_path),
            owner_id=None,
            twin_id=None,
            meeting_id=meeting_id,
            episode_uuid=episode_uuid,
            settings=settings,
        )
    assert opened == []


@pytest.mark.parametrize(
    "argv,target,expected",
    [
        (
            ["memory-add", "A note.", "--persona", "p.json", "--owner-id", "o", "--twin-id", "t"],
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

    monkeypatch.setattr(cli_main, target, fake_command)
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    cli_main.main()

    assert {key: captured[key] for key in expected} == expected


def test_memory_add_parses_optional_source_and_note_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_command(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "add_owner_note", fake_command)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fakeyou",
            "memory-add",
            "A note.",
            "--persona",
            "p.json",
            "--source-at",
            "2026-09-30T06:40:00+00:00",
            "--note-id",
            "b322d326-37b5-482a-904c-ef982f4dc510",
        ],
    )
    cli_main.main()

    assert captured["source_at"] == datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    assert captured["note_id"] == UUID("b322d326-37b5-482a-904c-ef982f4dc510")


def test_memory_add_defaults_source_and_note_id_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_command(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "add_owner_note", fake_command)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "memory-add", "A note.", "--persona", "p.json"])
    cli_main.main()

    assert captured["source_at"] is None
    assert captured["note_id"] is None


def test_memory_correct_parses_target_source_and_note_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    async def fake_command(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "correct_owner_note", fake_command)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fakeyou",
            "memory-correct",
            "Fixed note.",
            "--episode",
            "a1b2c3d4-0000-4000-8000-000000000001",
            "--persona",
            "p.json",
            "--source-at",
            "2026-09-30T06:40:00+00:00",
            "--note-id",
            "a1b2c3d4-0000-4000-8000-000000000002",
        ],
    )
    cli_main.main()

    assert captured["text"] == "Fixed note."
    assert captured["episode_uuid"] == "a1b2c3d4-0000-4000-8000-000000000001"
    assert captured["source_at"] == datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    assert captured["note_id"] == UUID("a1b2c3d4-0000-4000-8000-000000000002")
    assert captured["persona_path"] == Path("p.json")


@pytest.mark.parametrize(
    "argv",
    [
        ["memory-add", "A note.", "--persona", "p.json", "--source-at", "2026-09-30T06:40:00"],
        ["memory-add", "A note.", "--persona", "p.json", "--source-at", "not-a-timestamp"],
        ["memory-add", "A note.", "--persona", "p.json", "--note-id", "not-a-uuid"],
        [
            "memory-correct",
            "Fixed.",
            "--episode",
            "a1b2c3d4-0000-4000-8000-000000000001",
            "--persona",
            "p.json",
            "--source-at",
            "2026-09-30T06:40:00",
        ],
    ],
)
def test_memory_commands_reject_invalid_source_or_note_id_at_parse_time(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == 2


def test_memory_forget_rejects_both_targets_at_parse_time(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "memory-forget", "--meeting", "m1", "--episode", "e1", "--persona", "p"],
    )
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 2


def test_main_reports_unavailable_memory_with_infra_exit_code(monkeypatch, capsys) -> None:
    from fakeyou.provider_faults import ProviderFault

    async def unavailable(*_args, **_kwargs):
        raise ProviderFault("memory", "unavailable")

    monkeypatch.setattr(cli_main, "add_owner_note", unavailable)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "memory-add", "x", "--persona", "p.json"])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == 3
    assert "memory unavailable" in capsys.readouterr().err


async def test_join_hung_recall_times_out_as_unknown_and_keeps_listening(
    join,
    settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings.memory.recall.strategy = "tool"
    graphiti = make_graphiti(search_gate=asyncio.Event())
    model = recall_then_reply("I don't know yet, I'll check with Rani.")
    settings.memory.recall.timeout_seconds = 0.01

    _created, audio, outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    tool_results = [m for m in model.seen[-1] if isinstance(m, ToolMessage)]
    assert [m.content for m in tool_results] == [memory_module.RECALL_UNAVAILABLE_RESULT]
    assert graphiti.search_cancelled.is_set()
    assert audio.spoken == ["I don't know yet, I'll check with Rani."]
    assert outcome["phase"] == "listening"
    faults = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith('{"event":"fault_recorded"')
    ]
    assert [(row["source"], row["kind"]) for row in faults] == [("memory", "timeout")]


async def test_join_journals_session_result(session_run, join, tmp_path: Path) -> None:
    session_run.result.update(
        {"phase": "ended", "terminal_reason": "infra_failure", "detail": "dns"}
    )

    with pytest.raises(SystemExit):
        await join(
            MEET_LINK,
        )

    rows = [
        json.loads(line)
        for line in (tmp_path / "journals" / "meeting-1.jsonl").read_text().splitlines()
    ]
    [result] = [row for row in rows if row["event"] == "session_finished"]
    assert (result["terminal_reason"], result["exit_code"]) == ("infra_failure", 3)


def test_report_command_prints_summary_from_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journal_dir = tmp_path / "journals"
    journal_dir.mkdir()
    (journal_dir / "meeting-1.jsonl").write_text(
        json.dumps(
            {
                "event": "session_finished",
                "meeting_id": "meeting-1",
                "at": "2026-09-30T10:00:00+00:00",
                "phase": "ended",
                "terminal_reason": "stopped_by_owner",
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

    cli_main.main()

    report = json.loads(capsys.readouterr().out)
    assert report["meeting_id"] == "meeting-1"
    assert report["result"]["terminal_reason"] == "stopped_by_owner"


@pytest.mark.parametrize("meeting_id,match", [("missing", "no journal"), ("bad id", "pattern")])
def test_report_command_rejects_unknown_or_invalid_meeting(
    settings, tmp_path: Path, meeting_id: str, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        cli_diagnostics.show_session_report(meeting_id, tmp_path, settings)


def test_chat_model_uses_role_configuration(settings, monkeypatch):
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


def test_chat_model_forwards_google_reasoning_effort(settings, monkeypatch):
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


def test_chat_model_passes_provider_request_fields_as_extra_body(settings, monkeypatch):
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


def test_settings_read_extra_body_as_json(settings, monkeypatch):
    monkeypatch.setenv("FAKEYOU_REPLY__PROVIDER", "openai_compatible")
    monkeypatch.setenv("FAKEYOU_REPLY__BASE_URL", "https://api.minimax.io/v1")
    monkeypatch.setenv("FAKEYOU_REPLY__EXTRA_BODY", '{"reasoning_split": true}')
    assert Settings(_env_file=None).reply.extra_body == {"reasoning_split": True}


def test_chat_model_uses_its_own_key_and_explicit_provider(settings, monkeypatch):
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
def test_preflight_hosts_follow_the_configured_chat_endpoint(settings, endpoint):
    settings.reply.base_url = endpoint
    assert providers.preflight_hosts(settings) == (*BASE_HOSTS, endpoint)


def test_minimax_speech_needs_its_own_key_and_is_probed_before_join(settings):
    settings.tts = MiniMaxTTS()
    settings.reply = CompatibleReply(model="MiniMax-M3", base_url="https://api.minimax.io/v1")
    with pytest.raises(ValueError, match="FAKEYOU_TTS__API_KEY"):
        create_speech_synthesizer(settings.tts)
    settings.tts.api_key = SecretStr("minimax-key")
    assert isinstance(create_speech_synthesizer(settings.tts), MiniMaxSpeechSynthesizer)
    assert providers.preflight_hosts(settings) == (
        "https://api.minimax.io/v1/t2a_v2",
        "wss://api.meta.ai/v1/asr/realtime",
        "https://api.minimax.io/v1",
    )


def test_openai_speech_requires_its_own_key(settings):
    settings.tts.api_key = None
    with pytest.raises(ValueError, match="FAKEYOU_TTS__API_KEY"):
        create_speech_synthesizer(settings.tts)


def test_check_reports_the_speech_provider_defaults(settings, capsys):
    settings.tts = MiniMaxTTS()
    cli_diagnostics.check_setup(settings)
    assert json.loads(capsys.readouterr().out)["tts"] == settings.tts.model_dump(mode="json")


def test_memory_config_treats_a_blank_embedding_endpoint_as_the_default(settings):
    settings.memory.embedder = EmbedderSettings(base_url="", api_key=SecretStr("local"))
    assert settings.memory.embedder.base_url is None
    assert (
        providers.memory_config(settings).embedder.resolved_base_url == "https://api.openai.com/v1"
    )


def test_memory_config_routes_embeddings_and_extraction_separately(settings) -> None:
    settings.memory.embedder.base_url = "http://embeddings/v1"
    settings.memory.embedder.api_key = SecretStr("local")
    settings.memory.embedder.dim = 768
    settings.memory.llm.base_url = "https://api.meta.ai/v1"
    settings.memory.llm.api_key = SecretStr("meta-key")

    config = providers.memory_config(settings)

    assert (
        config.embedder.resolved_base_url,
        config.embedder.api_key.get_secret_value(),
        config.embedder.dim,
    ) == (
        "http://embeddings/v1",
        "local",
        768,
    )
    assert (config.llm.resolved_base_url, config.llm.api_key.get_secret_value()) == (
        "https://api.meta.ai/v1",
        "meta-key",
    )


def test_check_reports_models_per_role(settings, capsys):
    cli_diagnostics.check_setup(settings)
    report = capsys.readouterr().out
    assert json.loads(report) == settings.model_dump(mode="json")
    assert "test-key" not in report
    assert "memory-key" not in report


def write_record(settings: Settings, meeting_id: str) -> None:
    from fakeyou.conversation import TranscriptEvent
    from fakeyou.record import MeetingRecord

    record = MeetingRecord(meeting_record_path(settings.paths.record_dir, meeting_id))
    record.add_transcript(
        TranscriptEvent(
            id="t1",
            timestamp="2026-09-30T10:00:00+00:00",
            source="meeting_audio",
            text="The Orchid vendor kickoff is on Tuesday.",
            finalized=True,
            participant_id="unknown",
            speaker_evidence_id=None,
        )
    )
    record.add_twin_speech("Got it, Tuesday.")
    record.close()


async def test_memory_ingest_command_ingests_the_record_into_the_persona_group(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)

    await cli_memory.ingest_recorded_meeting(
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
    journal = (settings.paths.journal_dir / "meeting-9.jsonl").read_text()
    assert '"event":"memory_ingest_finished"' in journal


async def test_memory_ingest_command_exits_1_when_ingestion_is_incomplete(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_record(settings, "meeting-9")
    graphiti = make_graphiti()

    async def failing_add(**kwargs):
        raise RuntimeError("graph down")

    graphiti.add_episode = failing_add
    fake_persona_memory(monkeypatch, graphiti)

    with pytest.raises(SystemExit) as caught:
        await cli_memory.ingest_recorded_meeting(
            meeting_id="meeting-9",
            persona_path=write_persona(tmp_path),
            owner_id=None,
            twin_id=None,
            twin_name=None,
            settings=settings,
        )
    assert caught.value.code == 1


async def test_memory_ingest_command_requires_a_record(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(cli_errors.CliError, match="no meeting record"):
        await cli_memory.ingest_recorded_meeting(
            meeting_id="meeting-9",
            persona_path=write_persona(tmp_path),
            owner_id=None,
            twin_id=None,
            twin_name=None,
            settings=settings,
        )
    assert opened == []


async def test_memory_forget_keep_record_requires_a_meeting(settings, tmp_path: Path) -> None:
    with pytest.raises(cli_errors.CliError, match="--keep-record"):
        await cli_memory.forget_memory(
            persona_path=write_persona(tmp_path),
            owner_id=None,
            twin_id=None,
            meeting_id=None,
            episode_uuid="episode-7",
            settings=settings,
            keep_record=True,
        )


async def test_join_passes_persona_vocabulary_as_speech_keywords(
    session_run, join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *_args, **_kwargs: object())
    captured = await _join_with_audio(
        tmp_path,
        monkeypatch,
        conversation_enabled=True,
        persona_path=write_persona(tmp_path, vocabulary=["Orchid", "Budi"]),
        settings=settings,
        join=join,
        session_run=session_run,
    )

    assert captured["open_audio"].keywords["stt_keywords"] == ("Rani Twin", "Orchid", "Budi")


async def test_join_without_persona_has_no_speech_keywords(
    session_run, join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = await _join_with_audio(
        tmp_path, monkeypatch, settings=settings, join=join, session_run=session_run
    )

    assert captured["open_audio"].keywords["stt_keywords"] is None


def with_muse_fallback(settings: Settings) -> Settings:
    settings.reply.max_retries = 0
    settings.reply_fallback = CompatibleReply(
        model="muse-spark-1.2",
        base_url="https://api.meta.ai/v1",
        api_key=SecretStr("meta-key"),
        reasoning_effort="minimal",
        max_retries=1,
    )
    return settings


def test_primary_and_fallback_use_explicit_retry_budgets(settings, monkeypatch):
    calls = record_chat_model_calls(monkeypatch)
    settings = with_muse_fallback(settings)
    providers.create_chat_model(settings)
    providers.create_fallback_chat_model(settings)
    assert [kwargs["max_retries"] for _args, kwargs in calls] == [0, 1]
    assert [args for args, _kwargs in calls] == [("model",), ("muse-spark-1.2",)]


def test_fallback_chat_model_is_optional(settings) -> None:
    assert providers.create_fallback_chat_model(settings) is None


async def test_conversation_adds_model_fallback_middleware_when_configured(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from langchain.agents.middleware import ModelFallbackMiddleware

    created: dict = {}

    def fake_agent(model, **kwargs):
        created.update(kwargs)
        return SimpleNamespace(name="agent")

    monkeypatch.setattr(assembly, "create_meeting_agent", fake_agent)
    monkeypatch.setattr(providers, "init_chat_model", lambda *args, **kwargs: SimpleNamespace())
    settings = with_muse_fallback(settings)
    config = SimpleNamespace(owner_id="o", twin_id="t", meeting_id="m")

    async with assembly.open_conversation(settings, config, on_transition=lambda *args: None):
        pass

    greeting_type = type(create_greeting_middleware("id"))
    assert [type(item) for item in created["middleware"]] == [
        greeting_type,
        ModelFallbackMiddleware,
    ]


async def test_open_conversation_wires_sentence_streaming_flag(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.conversation import LangGraphResponder, StreamingLangGraphResponder

    seen: dict = {}

    class StubLoop:
        async def close(self) -> None:
            pass

    def fake_loop(responder, **kwargs):
        seen["responder"] = responder
        seen["kwargs"] = kwargs
        return StubLoop()

    monkeypatch.setattr(
        assembly, "create_meeting_agent", lambda *args, **kwargs: SimpleNamespace(name="agent")
    )
    monkeypatch.setattr(providers, "init_chat_model", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(assembly, "ConversationLoop", fake_loop)
    config = SimpleNamespace(owner_id="o", twin_id="t", meeting_id="m")

    async with assembly.open_conversation(settings, config, on_transition=lambda *args: None):
        pass
    assert type(seen["responder"]) is LangGraphResponder
    assert seen["kwargs"]["sentence_streaming"] is False

    settings.conversation.sentence_streaming = True
    async with assembly.open_conversation(settings, config, on_transition=lambda *args: None):
        pass
    assert isinstance(seen["responder"], StreamingLangGraphResponder)
    assert seen["kwargs"]["sentence_streaming"] is True


def test_preflight_probes_the_fallback_endpoint(settings):
    settings = with_muse_fallback(settings)
    settings.reply_fallback.base_url = "https://fallback.example.com:8443/v1"
    assert providers.preflight_hosts(settings) == (
        *BASE_HOSTS,
        "https://fallback.example.com:8443/v1",
    )


def test_check_reports_the_fallback_model(settings, capsys):
    settings = with_muse_fallback(settings)
    cli_diagnostics.check_setup(settings)
    assert json.loads(capsys.readouterr().out)[
        "reply_fallback"
    ] == settings.reply_fallback.model_dump(mode="json")


def test_check_reports_no_fallback_by_default(settings, capsys):
    cli_diagnostics.check_setup(settings)
    assert json.loads(capsys.readouterr().out)["reply_fallback"] is None


async def test_join_routes_interim_transcripts_to_barge_in(
    session_run, join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stops: list[bool] = []

    async def stop_playback():
        stops.append(True)

    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *args, **kwargs: object())

    async def run(config, **kwargs):
        loop = kwargs["conversation"]
        loop.bind(
            SimpleNamespace(
                speak=lambda text: asyncio.sleep(0),
                is_speaking=True,
                stop_playback=stop_playback,
            )
        )
        interim = kwargs["open_audio"].keywords["on_interim"]
        await interim(SimpleNamespace(text="wait"))
        await interim(SimpleNamespace(text="wait a moment"))
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    session_run.effect = run

    await _join_with_audio(
        tmp_path,
        monkeypatch,
        conversation_enabled=True,
        barge_in=True,
        settings=settings,
        join=join,
        session_run=session_run,
    )

    assert stops == [True]


async def test_memory_forget_should_delete_lifecycle_and_preserve_other_meetings(
    settings, tmp_path, monkeypatch
):
    from fakeyou.lifecycle import meeting_thread_id

    settings, thread_id = await _forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=False, settings=settings
    )
    profile = PersonaProfile.model_validate(PERSONA_DATA)

    assert not await _thread_exists(settings, thread_id)
    assert not await _thread_exists(settings, meeting_thread_id(profile.twin_id, "meeting-9"))
    assert await _thread_exists(settings, meeting_thread_id(profile.twin_id, "meeting-10"))


async def test_join_rejects_persona_language_mismatch_before_resources(
    join, settings, tmp_path, monkeypatch
):
    _enable_audio_platform(monkeypatch)
    settings.stt.api_key = SecretStr("muse-key")
    forbid_resources(monkeypatch)
    persona = write_persona(tmp_path, language="en")
    with pytest.raises(cli_errors.CliError, match="persona language"):
        await join(MEET_LINK, audio_enabled=True, conversation_enabled=True, persona_path=persona)
    assert not settings.paths.journal_dir.exists()


def printed_join_summary(capsys) -> dict:
    out = capsys.readouterr().out
    return json.loads(out[out.rindex('{\n  "meeting_id"') :])


def write_ingest_windows(settings, meeting_id: str, count: int) -> None:
    from datetime import timedelta

    from fakeyou.conversation import TranscriptEvent
    from fakeyou.record import MeetingRecord

    start = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
    moments = iter(start + timedelta(seconds=61 * index) for index in range(count))
    record = MeetingRecord(
        meeting_record_path(settings.paths.record_dir, meeting_id), now=lambda: next(moments)
    )
    for index in range(count):
        moment = start + timedelta(seconds=61 * index)
        record.add_transcript(
            TranscriptEvent(
                id=f"window-{index}",
                timestamp=moment.isoformat(),
                source="meeting_audio",
                text=f"Decision {index} is final.",
                finalized=True,
                participant_id="unknown",
                speaker_evidence_id=None,
            )
        )
    record.close()


async def test_join_leaves_pending_status_with_hash_and_no_auto_ingest(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import hashlib

    from fakeyou.ingestion_status import lock_path, read_status, status_path

    graphiti = make_graphiti()
    model = ToolCallingFakeModel(responses=[AIMessage(content="The demo moves to Thursday.")])

    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        transcripts=("When is the Orchid demo now?",),
        persona_overrides={"ingest_transcripts": True},
        settings=settings,
        join=join,
    )

    assert graphiti.added == []
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    status = read_status(status_path(record_path))
    profile = PersonaProfile.model_validate({**PERSONA_DATA, "ingest_transcripts": True})
    assert status is not None
    assert status.status == "pending"
    assert status.group_id == profile.memory_group
    assert status.meeting_id == "meeting-9"
    assert status.record_sha256 == hashlib.sha256(record_path.read_bytes()).hexdigest()
    assert status.windows == 1
    assert not lock_path(record_path).exists()
    printed = printed_join_summary(capsys)
    assert printed["session_outcome"] == "completed"
    assert printed["ingestion_outcome"] == "pending"
    assert printed["record_error"] is None
    assert "memory-ingest --meeting meeting-9 --persona" in printed["resume_command"]
    assert "persona.json" in printed["resume_command"]


async def test_join_with_hanging_graphiti_leaves_without_ingesting(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = make_graphiti()
    graphiti.gate = asyncio.Event()
    model = ToolCallingFakeModel(responses=[AIMessage(content="Noted.")])

    await asyncio.wait_for(
        _join_persona_conversation(
            tmp_path,
            monkeypatch,
            model=model,
            graphiti=graphiti,
            memory=True,
            transcripts=("When is the Orchid demo now?",),
            persona_overrides={"ingest_transcripts": True},
            settings=settings,
            join=join,
        ),
        timeout=20,
    )

    assert graphiti.added == []


async def test_join_without_ingest_intent_writes_not_requested(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from fakeyou.ingestion_status import read_status, status_path

    graphiti = make_graphiti()
    model = ToolCallingFakeModel(responses=[AIMessage(content="Okay.")])

    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    status = read_status(status_path(meeting_record_path(settings.paths.record_dir, "meeting-9")))
    assert status is not None
    assert status.status == "not_requested"
    assert status.record_sha256 is None
    printed = printed_join_summary(capsys)
    assert printed["ingestion_outcome"] == "not_requested"
    assert printed["resume_command"] is None


async def test_join_without_persona_writes_no_status(
    session_run, join, settings, tmp_path: Path, capsys
) -> None:
    from fakeyou.ingestion_status import status_path

    await join(MEET_LINK)

    record_path = meeting_record_path(settings.paths.record_dir, "meeting-1")
    assert not status_path(record_path).exists()
    printed = printed_join_summary(capsys)
    assert printed["session_outcome"] == "completed"
    assert printed["ingestion_outcome"] == "not_requested"
    assert printed["record_error"] is None
    assert printed["resume_command"] is None


async def test_join_record_finalize_failure_is_visible_and_exits_1(
    session_run, join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import os

    calls = 0

    def fail_after_lock_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("disk unavailable")

    monkeypatch.setattr(os, "fsync", fail_after_lock_fsync)

    with pytest.raises(SystemExit) as caught:
        await join(MEET_LINK)
    assert caught.value.code == 1

    printed = printed_join_summary(capsys)
    assert printed["session_outcome"] == "completed"
    assert printed["ingestion_outcome"] == "not_requested"
    assert "record" in printed["record_error"]
    rows = [
        json.loads(line)
        for line in (tmp_path / "journals" / "meeting-1.jsonl").read_text().splitlines()
    ]
    finished = [row for row in rows if row["event"] == "session_finished"]
    exited = [row for row in rows if row["event"] == "cli_exited"]
    assert [row["exit_code"] for row in finished] == [0]
    assert [row["exit_code"] for row in exited] == [1]


async def test_join_ingest_requested_but_finalize_failed_reports_unknown(
    join, settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import os

    calls = 0

    def fail_at_record_close(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls > 3:
            raise OSError("disk unavailable")

    monkeypatch.setattr(os, "fsync", fail_at_record_close)
    graphiti = make_graphiti()
    model = ToolCallingFakeModel(responses=[AIMessage(content="Okay.")])

    with pytest.raises(SystemExit) as caught:
        await _join_persona_conversation(
            tmp_path,
            monkeypatch,
            model=model,
            graphiti=graphiti,
            memory=True,
            persona_overrides={"ingest_transcripts": True},
            settings=settings,
            join=join,
        )
    assert caught.value.code == 1

    printed = printed_join_summary(capsys)
    assert printed["ingestion_outcome"] == "unknown"
    assert "record" in printed["record_error"]
    assert printed["resume_command"] is None


async def test_memory_ingest_recovers_pending_without_hash_after_crash(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import hashlib

    from fakeyou.ingestion_status import IngestionStatus, read_status, status_path, write_status

    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    path = status_path(record_path)
    write_status(
        path,
        IngestionStatus.model_validate(
            {
                "meeting_id": "meeting-9",
                "group_id": profile.memory_group,
                "status": "pending",
                "windows": 0,
                "resumed_from": 0,
                "ingested": 0,
                "record_sha256": None,
                "updated_at": datetime(2026, 10, 3, 8, tzinfo=UTC).isoformat(),
                "reason": None,
            }
        ),
    )

    await cli_memory.ingest_recorded_meeting(
        meeting_id="meeting-9",
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        twin_name=None,
        settings=settings,
    )

    assert [episode["name"] for episode in graphiti.added] == ["meeting-9:w00000"]
    status = read_status(path)
    assert status is not None
    assert status.status == "completed"
    assert status.record_sha256 == hashlib.sha256(record_path.read_bytes()).hexdigest()
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert (summary["ingested"], summary["completed"]) == (1, True)


async def test_memory_ingest_failure_then_resumes_from_native_progress(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from fakeyou.ingestion_status import read_status, status_path

    write_ingest_windows(settings, "meeting-9", 3)
    graphiti = make_graphiti(episode_prefix="uuid-")
    graphiti.errors[1] = RuntimeError("graph down")
    fake_persona_memory(monkeypatch, graphiti)
    arguments = {
        "meeting_id": "meeting-9",
        "persona_path": write_persona(tmp_path),
        "owner_id": None,
        "twin_id": None,
        "twin_name": None,
        "settings": settings,
    }

    with pytest.raises(SystemExit) as first:
        await cli_memory.ingest_recorded_meeting(**arguments)
    assert first.value.code == 1
    path = status_path(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    failed = read_status(path)
    assert failed is not None and failed.status == "failed"

    await cli_memory.ingest_recorded_meeting(**arguments)

    assert [episode["name"] for episode in graphiti.added] == [
        "meeting-9:w00000",
        "meeting-9:w00001",
        "meeting-9:w00002",
    ]
    resumed = read_status(path)
    assert resumed is not None
    assert (resumed.status, resumed.windows, resumed.resumed_from, resumed.ingested) == (
        "completed",
        3,
        1,
        2,
    )
    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert (summary["ingested"], summary["completed"]) == (2, True)


async def test_memory_ingest_repeat_after_completed_adds_no_episodes(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    arguments = {
        "meeting_id": "meeting-9",
        "persona_path": write_persona(tmp_path),
        "owner_id": None,
        "twin_id": None,
        "twin_name": None,
        "settings": settings,
    }

    await cli_memory.ingest_recorded_meeting(**arguments)
    await cli_memory.ingest_recorded_meeting(**arguments)

    assert [episode["name"] for episode in graphiti.added] == ["meeting-9:w00000"]


async def test_memory_ingest_refuses_an_active_writer_lock(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.ingestion_status import record_lock

    write_record(settings, "meeting-9")
    opened = fake_persona_memory(monkeypatch, make_graphiti())
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")

    with record_lock(record_path, purpose="meeting"):
        with pytest.raises(cli_errors.CliError, match="locked"):
            await cli_memory.ingest_recorded_meeting(
                meeting_id="meeting-9",
                persona_path=write_persona(tmp_path),
                owner_id=None,
                twin_id=None,
                twin_name=None,
                settings=settings,
            )
    assert opened == []
    assert record_path.exists()


async def test_memory_ingest_rejects_a_changed_record_hash(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.conversation import TranscriptEvent
    from fakeyou.ingestion_status import read_status, status_path
    from fakeyou.record import MeetingRecord

    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    arguments = {
        "meeting_id": "meeting-9",
        "persona_path": write_persona(tmp_path),
        "owner_id": None,
        "twin_id": None,
        "twin_name": None,
        "settings": settings,
    }
    await cli_memory.ingest_recorded_meeting(**arguments)

    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    record = MeetingRecord(record_path)
    record.add_transcript(
        TranscriptEvent(
            id="late",
            timestamp="2026-09-30T11:00:00+00:00",
            source="meeting_audio",
            text="A late change.",
            finalized=True,
            participant_id="unknown",
            speaker_evidence_id=None,
        )
    )
    record.close()

    with pytest.raises(SystemExit) as caught:
        await cli_memory.ingest_recorded_meeting(**arguments)
    assert caught.value.code == 1
    assert len(graphiti.added) == 1
    status = read_status(status_path(record_path))
    assert status is not None and status.status == "failed"


async def test_memory_ingest_rejects_a_foreign_group_status(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.ingestion_status import IngestionStatus, read_status, status_path, write_status

    write_record(settings, "meeting-9")
    opened = fake_persona_memory(monkeypatch, make_graphiti())
    path = status_path(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    write_status(
        path,
        IngestionStatus.model_validate(
            {
                "meeting_id": "meeting-9",
                "group_id": "twin_other",
                "status": "pending",
                "windows": 1,
                "resumed_from": 0,
                "ingested": 0,
                "record_sha256": None,
                "updated_at": datetime(2026, 10, 3, 8, tzinfo=UTC).isoformat(),
                "reason": None,
            }
        ),
    )

    with pytest.raises(cli_errors.CliError, match="group"):
        await cli_memory.ingest_recorded_meeting(
            meeting_id="meeting-9",
            persona_path=write_persona(tmp_path),
            owner_id=None,
            twin_id=None,
            twin_name=None,
            settings=settings,
        )
    assert opened == []
    status = read_status(path)
    assert status is not None
    assert (status.status, status.group_id) == ("pending", "twin_other")


async def test_memory_ingest_corrupt_status_starts_fresh(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.ingestion_status import read_status, status_path

    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    path = status_path(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt", encoding="utf-8")

    await cli_memory.ingest_recorded_meeting(
        meeting_id="meeting-9",
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        twin_name=None,
        settings=settings,
    )

    assert len(graphiti.added) == 1
    status = read_status(path)
    assert status is not None and status.status == "completed"


async def test_memory_ingest_partial_record_is_not_completed(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.ingestion_status import read_status, status_path

    write_record(settings, "meeting-9")
    opened = fake_persona_memory(monkeypatch, make_graphiti())
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write("{partial\n")

    with pytest.raises(SystemExit) as caught:
        await cli_memory.ingest_recorded_meeting(
            meeting_id="meeting-9",
            persona_path=write_persona(tmp_path),
            owner_id=None,
            twin_id=None,
            twin_name=None,
            settings=settings,
        )
    assert caught.value.code == 1
    assert opened == []
    status = read_status(status_path(record_path))
    assert status is not None and status.status == "failed"


async def test_memory_ingest_empty_record_reports_zero_windows(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from fakeyou.ingestion_status import read_status, status_path
    from fakeyou.record import MeetingRecord

    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    MeetingRecord(record_path).close()
    fake_persona_memory(monkeypatch, make_graphiti())

    await cli_memory.ingest_recorded_meeting(
        meeting_id="meeting-9",
        persona_path=write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        twin_name=None,
        settings=settings,
    )

    summary = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert (summary["windows"], summary["ingested"], summary["completed"]) == (0, 0, True)
    status = read_status(status_path(record_path))
    assert status is not None
    assert (status.status, status.windows) == ("completed", 0)


async def test_memory_ingest_stop_reports_interrupted_and_exits_1(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.ingestion_status import read_status, status_path

    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    real_ingest = cli_memory.ingest_until_stopped

    async def stop_first(*args, **kwargs):
        kwargs["stop"].set()
        return await real_ingest(*args, **kwargs)

    monkeypatch.setattr(cli_memory, "ingest_until_stopped", stop_first)

    with pytest.raises(SystemExit) as caught:
        await cli_memory.ingest_recorded_meeting(
            meeting_id="meeting-9",
            persona_path=write_persona(tmp_path),
            owner_id=None,
            twin_id=None,
            twin_name=None,
            settings=settings,
        )
    assert caught.value.code == 1
    assert graphiti.added == []
    status = read_status(status_path(meeting_record_path(settings.paths.record_dir, "meeting-9")))
    assert status is not None and status.status == "interrupted"


async def test_ingest_until_stopped_uses_the_caller_stop_event(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.faults import FaultReporter
    from tests.fakes import RecordingJournal

    write_record(settings, "meeting-9")
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    before = signal.getsignal(signal.SIGINT)

    stopped = await cli_memory.ingest_until_stopped(
        make_graphiti(),
        record_path,
        profile=profile,
        meeting_id="meeting-9",
        twin_name="Rani Twin",
        journal=journal,
        faults=faults,
        settings=settings,
        stop=asyncio.Event(),
    )
    assert (stopped.ingested, stopped.completed) == (1, True)

    halted = asyncio.Event()
    halted.set()
    early = await cli_memory.ingest_until_stopped(
        make_graphiti(),
        record_path,
        profile=profile,
        meeting_id="meeting-9",
        twin_name="Rani Twin",
        journal=journal,
        faults=faults,
        settings=settings,
        stop=halted,
    )
    assert (early.ingested, early.completed) == (0, False)
    assert signal.getsignal(signal.SIGINT) == before
    journal.close()


@pytest.mark.parametrize("lock_purpose", ["meeting", "ingestion", "debrief"])
@pytest.mark.parametrize("keep_record", [False, True])
async def test_memory_forget_meeting_refuses_an_active_lock(
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep_record: bool, lock_purpose: str
) -> None:
    from fakeyou.ingestion_status import record_lock

    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}\n", encoding="utf-8")
    context = expected_context(meeting_brief(), PersonaProfile.model_validate(PERSONA_DATA))
    write_meeting_context(context_path(record_path), context)
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    async def forget(memory, **kwargs):
        pytest.fail("provider must stay closed while the record is locked")

    monkeypatch.setattr(cli_memory, "forget_meeting", forget)

    with record_lock(record_path, purpose=lock_purpose):
        with pytest.raises(cli_errors.CliError, match="locked"):
            await cli_memory.forget_memory(
                persona_path=write_persona(tmp_path),
                owner_id=None,
                twin_id=None,
                meeting_id="meeting-9",
                episode_uuid=None,
                settings=settings,
                keep_record=keep_record,
            )
    assert opened == []
    assert record_path.exists()
    assert read_meeting_context(context_path(record_path)) == context


async def test_join_rejects_policy_without_wake_phrase(join, settings) -> None:
    settings.conversation.participant_policy_enabled = True
    with pytest.raises(cli_errors.CliError, match="wake-phrase"):
        await join("https://meet.google.com/abc-defg-hij", meeting_id=None)
