import asyncio
import json
import signal
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

import fakeyou.cli.join as cli_join
import fakeyou.cli.main as cli_main
import fakeyou.memory.memory as memory_module
import fakeyou.orchestration.assembly as assembly
import fakeyou.orchestration.providers as providers
from fakeyou.audio.playback import PlaybackEvent
from fakeyou.catalog.errors import CATALOG, ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.conversation.agent import DEFAULT_PERSONA
from fakeyou.conversation.conversation import (
    ConversationLoop,
    LangGraphResponder,
    conversation_thread_id,
)
from fakeyou.conversation.session import SessionResult
from fakeyou.foundation.greetings import create_greeting_middleware
from fakeyou.meet.browser import profile_lock
from fakeyou.persistence.record import (
    TranscriptEntry,
    TwinSpeechEntry,
    load_meeting_record,
    meeting_record_path,
)
from tests._cli_support import (
    MEET_LINK,
    _enable_audio_platform,
    _join_with_audio,
    forbid_resources,
    persona_join_arguments,
    printed_join_summary,
    write_persona,
)
from tests.fakes import (
    DATABASE_URL,
    SessionRun,
    make_graphiti,
)


@pytest.mark.asyncio
async def test_join_builds_session_config_and_prints_result(
    session_run: SessionRun,
    join: Callable[..., Any],
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
    session_run: SessionRun,
    join: Callable[..., Any],
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
    assert (
        config.browser_profile_dir
        == Path(".runtime/profiles") / "settings-twin" / config.meeting_id
    )
    assert config.lobby_timeout_seconds == 300.0
    assert config.session_duration_seconds == 7200.0
    assert len(config.meeting_id) == 12
    assert f'"meeting_id": "{config.meeting_id}"' in capsys.readouterr().out


@pytest.mark.parametrize(
    ("terminal_reason", "exit_code"),
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
    session_run: SessionRun,
    join: Callable[..., Any],
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
        assert caught is not None
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
    session_run: SessionRun,
    join: Callable[..., Any],
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
    session_run: SessionRun, join: Callable[..., Any], tmp_path: Path
) -> None:
    from fakeyou.observability.faults import FaultReporter
    from fakeyou.observability.journal import Journal

    captured = session_run

    session_run.result.update(
        {"phase": "ended", "terminal_reason": "stopped_by_owner", "detail": "ok"}
    )

    await join(
        "https://meet.google.com/abc-defg-hij",
    )

    assert captured["config"].database_url == DATABASE_URL
    assert isinstance(captured["faults"], FaultReporter)
    assert isinstance(captured["journal"], Journal)
    assert captured["preflight_hosts"] is None
    assert (tmp_path / "journals" / "meeting-1.jsonl").is_file()


@pytest.mark.parametrize(("audio_enabled", "speech"), [(False, "hello"), (True, " ")])
async def test_join_rejects_invalid_speech_before_browser_start(
    join: Callable[..., Any], audio_enabled: bool, speech: str
) -> None:
    with pytest.raises(FakeYouError) as caught:
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            audio_enabled=audio_enabled,
            initial_speech=speech,
        )
    assert caught.value.code is ErrorCode.MEETING_SPEAK_REQUIRES_AUDIO


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
async def test_join_rejects_invalid_link(join: Callable[..., Any], link: str) -> None:
    with pytest.raises(FakeYouError) as caught:
        await join(
            link,
            meeting_id=None,
        )
    assert caught.value.code is ErrorCode.MEETING_LINK_INVALID


@pytest.mark.parametrize(
    ("lobby_timeout", "duration", "code"),
    [
        (0.0, None, ErrorCode.MEETING_LOBBY_TIMEOUT_NOT_POSITIVE),
        (-1.0, None, ErrorCode.MEETING_LOBBY_TIMEOUT_NOT_POSITIVE),
        (float("nan"), None, ErrorCode.MEETING_LOBBY_TIMEOUT_NOT_POSITIVE),
        (None, 0.0, ErrorCode.MEETING_DURATION_NOT_POSITIVE),
        (None, -5.0, ErrorCode.MEETING_DURATION_NOT_POSITIVE),
    ],
)
async def test_join_rejects_nonpositive_timeouts(
    join: Callable[..., Any],
    lobby_timeout: float | None,
    duration: float | None,
    code: ErrorCode,
) -> None:
    with pytest.raises(FakeYouError) as caught:
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            lobby_timeout=lobby_timeout,
            duration=duration,
        )
    assert caught.value.code is code


async def test_join_audio_precondition_requires_the_worker(
    join: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PULSE_SOURCE", "wrong-source")
    with pytest.raises(FakeYouError) as caught:
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            audio_enabled=True,
        )
    assert caught.value.code is ErrorCode.MEETING_AUDIO_REQUIRES_WORKER


async def test_join_headless_override(
    session_run: SessionRun, join: Callable[..., Any], settings: Settings
) -> None:
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


def test_join_owner_id_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_join(link: str, **kwargs: Any) -> None:
        captured["link"] = link
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "join_meeting", fake_join)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "join", "https://meet.google.com/abc-defg-hij", "--owner-id", "owner-9"],
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()
    assert exited.value.code == 0
    assert captured["options"].identity.owner_id == "owner-9"


@pytest.mark.parametrize(
    "command", ["join", "memory-add", "memory-forget", "memory-ingest", "memory-reembed"]
)
def test_cli_rejects_owner_alias(
    command: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = {
        "join": ["https://meet.google.com/abc-defg-hij"],
        "memory-add": ["A note.", "--persona", "p.json"],
        "memory-forget": ["--meeting-id", "meeting-1", "--persona", "p.json"],
        "memory-ingest": ["--meeting-id", "meeting-1", "--persona", "p.json"],
        "memory-reembed": ["--persona", "p.json"],
    }
    monkeypatch.setattr(sys, "argv", ["fakeyou", command, *arguments[command], "--owner", "o"])
    with pytest.raises(SystemExit) as error:
        cli_main.main()
    assert error.value.code == 2
    assert "No such option: --owner" in capsys.readouterr().err


@pytest.mark.parametrize(("argv", "expected"), [(["--headless"], True), ([], None)])
def test_join_headless_flag(
    monkeypatch: pytest.MonkeyPatch, argv: list[Any], expected: bool | None
) -> None:
    captured: dict[str, Any] = {}

    async def fake_join(link: str, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "join_meeting", fake_join)
    monkeypatch.setattr(
        sys, "argv", ["fakeyou", "join", "https://meet.google.com/abc-defg-hij", *argv]
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()
    assert exited.value.code == 0
    assert captured["options"].headless is expected


@pytest.mark.asyncio
async def test_join_audio_builds_conversation_with_meeting_thread(
    session_run: SessionRun,
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[str, Any] = {}

    def fake_agent(
        model: Any,
        *,
        tools: tuple[Any, ...] = (),
        checkpointer: Any = None,
        **kwargs: Any,
    ) -> SimpleNamespace:
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
    responder = loop._responder
    assert isinstance(responder, LangGraphResponder)
    assert responder.journal is not None


@pytest.mark.asyncio
async def test_join_audio_transcript_reaches_conversation(
    session_run: SessionRun,
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    agent_calls: list[Any] = []

    class FakeAgent:
        async def ainvoke(
            self, request: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            agent_calls.append(config)
            return {"messages": [AIMessage(content="Reply.")]}

    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *args, **kwargs: FakeAgent())
    captured = {}

    async def run(config: Any, **kwargs: Any) -> SessionResult:
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
        await kwargs["open_audio"].keywords["callbacks"].on_transcript(frame)
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
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith('{"')
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
    transcripts = cast(list[TranscriptEntry], entries)
    assert [(entry.kind, entry.text) for entry in transcripts] == [("transcript", "Hello.")]


async def test_audio_only_does_not_construct_responding_agent(
    session_run: SessionRun,
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        assembly,
        "create_meeting_agent",
        lambda *args, **kwargs: pytest.fail("agent must be opt-in"),
    )
    captured = await _join_with_audio(
        tmp_path, monkeypatch, settings=settings, join=join, session_run=session_run
    )
    assert captured["conversation"] is None


async def test_conversation_option_requires_audio(join: Callable[..., Any]) -> None:
    with pytest.raises(FakeYouError) as caught:
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            conversation_enabled=True,
        )
    assert caught.value.code is ErrorCode.MEETING_CONVERSE_REQUIRES_AUDIO


@pytest.mark.asyncio
async def test_join_memory_requires_converse(join: Callable[..., Any]) -> None:
    with pytest.raises(FakeYouError) as caught:
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            memory=True,
        )
    assert caught.value.code is ErrorCode.MEETING_MEMORY_REQUIRES_CONVERSE


@pytest.mark.asyncio
async def test_join_wake_phrase_requires_converse(join: Callable[..., Any]) -> None:
    with pytest.raises(FakeYouError) as caught:
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            wake_phrase="hey fakeyou",
        )
    assert caught.value.code is ErrorCode.MEETING_WAKE_PHRASE_REQUIRES_CONVERSE


@pytest.mark.asyncio
async def test_join_barge_in_requires_converse(join: Callable[..., Any]) -> None:
    with pytest.raises(FakeYouError) as caught:
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            barge_in=True,
        )
    assert caught.value.code is ErrorCode.MEETING_BARGE_IN_REQUIRES_CONVERSE


@pytest.mark.asyncio
async def test_join_wake_phrase_gates_agent_replies(
    session_run: SessionRun,
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_calls = []

    class FakeAgent:
        async def ainvoke(
            self, request: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            agent_calls.append(request)
            return {"messages": [AIMessage(content="Reply.")]}

    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *args, **kwargs: FakeAgent())

    async def run(config: Any, **kwargs: Any) -> SessionResult:
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
            await kwargs["open_audio"].keywords["callbacks"].on_transcript(frame)
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
    session_run: SessionRun,
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stops = []

    class FakeAgent:
        async def ainvoke(
            self, request: dict[str, Any], config: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            return {"messages": [AIMessage(content="Reply.")]}

    async def stop_playback() -> None:
        stops.append(True)

    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *args, **kwargs: FakeAgent())

    async def run(config: Any, **kwargs: Any) -> SessionResult:
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
        await kwargs["open_audio"].keywords["callbacks"].on_transcript(frame)
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
    session_run: SessionRun,
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def run(config: Any, **kwargs: Any) -> SessionResult:
        handler = kwargs["open_audio"].keywords["callbacks"].on_playback_event
        handler(PlaybackEvent("playback_started", "hello", ""))
        handler(PlaybackEvent("playback_dropped", "cut off", "interrupted"))
        handler(PlaybackEvent("playback_finished", "hello?  ", ""))
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    session_run.effect = run

    await _join_with_audio(
        tmp_path, monkeypatch, settings=settings, join=join, session_run=session_run
    )

    rows = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith('{"')
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
    speeches = cast(list[TwinSpeechEntry], entries)
    assert [(entry.kind, entry.text) for entry in speeches] == [("twin_speech", "hello?  ")]


@pytest.mark.parametrize(
    ("graphiti_options", "kind"),
    [
        ({"connect_error": ConnectionRefusedError("refused")}, "unknown"),
        ({"connect_gate": asyncio.Event()}, "timeout"),
    ],
)
async def test_join_memory_startup_failure_exits_before_browser(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    graphiti_options: dict[str, Any],
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
    async def tracked_saver(_value: Any, url: Any = None) -> AsyncIterator[InMemorySaver]:
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
    assert f'"kind":"{kind}"' in captured.err
    assert '"exit_code":3' in captured.err


async def test_join_refuses_a_profile_already_in_use(
    join: Callable[..., Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_resources(monkeypatch)
    profile = tmp_path / "profile"

    with (
        profile_lock(profile),
        pytest.raises(FakeYouError, match="browser profile is in use: lease is locked:") as caught,
    ):
        await join(MEET_LINK, profile_dir=str(profile))
    assert caught.value.code is ErrorCode.BROWSER_PROFILE_IN_USE


async def test_join_refuses_a_locked_meeting_record(
    join: Callable[..., Any], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.ingestion_status import record_lock

    forbid_resources(monkeypatch)
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-1")

    with record_lock(record_path, purpose="meeting"):
        with pytest.raises(FakeYouError, match="meeting record is locked:") as caught:
            await join(MEET_LINK)
    assert caught.value.code is ErrorCode.RECORD_LOCKED
    assert not settings.paths.journal_dir.exists()


async def test_join_journals_session_result(
    session_run: SessionRun, join: Callable[..., Any], tmp_path: Path
) -> None:
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


async def test_join_routes_interim_transcripts_to_barge_in(
    session_run: SessionRun,
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stops: list[bool] = []

    async def stop_playback() -> None:
        stops.append(True)

    monkeypatch.setattr(assembly, "create_meeting_agent", lambda *args, **kwargs: object())

    async def run(config: Any, **kwargs: Any) -> SessionResult:
        loop = kwargs["conversation"]
        loop.bind(
            SimpleNamespace(
                speak=lambda text: asyncio.sleep(0),
                is_speaking=True,
                stop_playback=stop_playback,
            )
        )
        interim = kwargs["open_audio"].keywords["callbacks"].on_interim
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


async def test_join_rejects_policy_without_wake_phrase(
    join: Callable[..., Any], settings: Settings
) -> None:
    settings.conversation.participant_policy_enabled = True
    with pytest.raises(FakeYouError, match="wake-phrase") as caught:
        await join("https://meet.google.com/abc-defg-hij", meeting_id=None)
    assert caught.value.code is ErrorCode.MEETING_PARTICIPANT_POLICY_REQUIRES_ALIASES


@pytest.mark.asyncio
async def test_join_preserves_infra_catalog_error_from_session(
    session_run: SessionRun, join: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing(_config: object, **_kwargs: object) -> object:
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE)

    session_run.effect = failing

    with pytest.raises(SystemExit) as caught:
        await join("https://meet.google.com/abc-defg-hij")

    assert caught.value.code == CATALOG[ErrorCode.DATABASE_UNREACHABLE].exit_code
    printed = printed_join_summary(capsys)
    assert printed["error_code"] == ErrorCode.DATABASE_UNREACHABLE.value
    assert printed["terminal_reason"] == "infra_failure"
    assert printed["detail"] == str(FakeYouError(ErrorCode.DATABASE_UNREACHABLE))
    assert printed["session_outcome"] == "failed"


@pytest.mark.asyncio
async def test_join_preserves_failure_catalog_error_from_session(
    session_run: SessionRun, join: Callable[..., Any], capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing(_config: object, **_kwargs: object) -> object:
        raise FakeYouError(ErrorCode.OPERATION_INTERRUPTED)

    session_run.effect = failing

    with pytest.raises(SystemExit) as caught:
        await join("https://meet.google.com/abc-defg-hij")

    assert caught.value.code == CATALOG[ErrorCode.OPERATION_INTERRUPTED].exit_code
    printed = printed_join_summary(capsys)
    assert printed["error_code"] == ErrorCode.OPERATION_INTERRUPTED.value
    assert printed["terminal_reason"] == "browser_failure"
    assert printed["detail"] == str(FakeYouError(ErrorCode.OPERATION_INTERRUPTED))
    assert printed["session_outcome"] == "failed"
