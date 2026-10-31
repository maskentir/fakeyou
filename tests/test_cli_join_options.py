import asyncio
import json
import signal
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import pytest

import fakeyou.cli.main as cli_main
import fakeyou.orchestration.join as join_service
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.persistence.ingestion_status import record_lock
from tests._error_support import message_pattern
from tests._runtime_fakes import SessionRun


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
async def test_join_installs_signal_handlers_for_resource_acquisition(
    session_run: SessionRun,
    join: Callable[..., Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del session_run
    from fakeyou.orchestration.assembly import SignalHandlers
    from fakeyou.orchestration.assembly import stop_signal_handlers as real_handlers

    entered: list[str] = []

    @contextmanager
    def recording(stop: asyncio.Event) -> Iterator[SignalHandlers]:
        entered.append("enter")
        with real_handlers(stop) as bound:
            yield bound
        entered.append("exit")

    @contextmanager
    def spying_record_lock(*args: Any, **kwargs: Any) -> Iterator[Any]:
        assert entered == ["enter"], "signal handlers must span resource acquisition"
        entered.append("record_lock")
        with record_lock(*args, **kwargs) as held:
            yield held

    monkeypatch.setattr(join_service, "stop_signal_handlers", recording)
    monkeypatch.setattr(join_service, "record_lock", spying_record_lock)

    await join("https://meet.google.com/abc-defg-hij")

    assert entered == ["enter", "record_lock", "exit"]


@pytest.mark.asyncio
async def test_join_falls_back_to_settings_and_generates_meeting_id(
    session_run: SessionRun,
    join: Callable[..., Any],
    settings: Settings,
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
        == settings.paths.browser_profile_dir / "settings-twin" / config.meeting_id
    )
    assert config.lobby_timeout_seconds == 300.0
    assert config.session_duration_seconds == 7200.0
    assert len(config.meeting_id) == 12
    assert f'"meeting_id": "{config.meeting_id}"' in capsys.readouterr().out


@pytest.mark.parametrize(
    "outcome",
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
    outcome: tuple[str | None, int],
) -> None:
    terminal_reason, exit_code = outcome
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

    assert isinstance(captured["faults"], FaultReporter)
    assert isinstance(captured["journal"], Journal)
    assert captured["preflight_hosts"] is None
    assert (tmp_path / "journals" / "meeting-1.jsonl").is_file()


@pytest.mark.parametrize(("audio_enabled", "speech"), [(False, "hello"), (True, " ")])
async def test_join_rejects_invalid_speech_before_browser_start(
    join: Callable[..., Any], *, audio_enabled: bool, speech: str
) -> None:
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEETING_SPEAK_REQUIRES_AUDIO)
    ) as caught:
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
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEETING_LINK_INVALID)
    ) as caught:
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
    with pytest.raises(
        FakeYouError,
        match=message_pattern(code),
    ) as caught:
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
    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.MEETING_AUDIO_REQUIRES_WORKER),
    ) as caught:
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

    async def fake_join(options: Any, **kwargs: Any) -> None:
        captured["options"] = options
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "run_session", fake_join)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "join", "https://meet.google.com/abc-defg-hij", "--owner-id", "owner-9"],
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()
    assert exited.value.code == 0
    assert captured["options"].owner_id == "owner-9"


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
        "memory-ingest": ["--meeting-id", "meeting-1"],
        "memory-reembed": ["--persona", "p.json"],
    }
    monkeypatch.setattr(sys, "argv", ["fakeyou", command, *arguments[command], "--owner", "o"])
    with pytest.raises(SystemExit) as error:
        cli_main.main()
    assert error.value.code == 2
    assert "No such option: --owner" in capsys.readouterr().err


@pytest.mark.parametrize(("argv", "expected"), [(["--headless"], True), ([], None)])
def test_join_headless_flag(
    monkeypatch: pytest.MonkeyPatch, argv: list[Any], *, expected: bool | None
) -> None:
    captured: dict[str, Any] = {}

    async def fake_join(options: Any, **kwargs: Any) -> None:
        captured["options"] = options
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "run_session", fake_join)
    monkeypatch.setattr(
        sys, "argv", ["fakeyou", "join", "https://meet.google.com/abc-defg-hij", *argv]
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()
    assert exited.value.code == 0
    assert captured["options"].headless is expected


@pytest.mark.asyncio
async def test_join_memory_requires_converse(join: Callable[..., Any]) -> None:
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEETING_MEMORY_REQUIRES_CONVERSE)
    ) as caught:
        await join(
            "https://meet.google.com/abc-defg-hij",
            meeting_id=None,
            memory=True,
        )
    assert caught.value.code is ErrorCode.MEETING_MEMORY_REQUIRES_CONVERSE
