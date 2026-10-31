import asyncio
import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr

import fakeyou.memory.memory as memory_module
import fakeyou.orchestration.assembly as assembly
import fakeyou.orchestration.providers as providers
import tests._join_runner as cli_join
from fakeyou.catalog.errors import CATALOG, ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.session import SessionConfig, SessionResult
from fakeyou.meet.browser import profile_lock
from fakeyou.memory.persona import OperatorIdentity
from fakeyou.orchestration.join import run_join_session
from fakeyou.orchestration.join_types import JoinPorts, JoinRequest
from fakeyou.orchestration.runtime import SessionWiring
from fakeyou.persistence.lease import LeaseError
from fakeyou.persistence.record import meeting_record_path
from tests._cli_support import (
    MEET_LINK,
    enable_audio_platform,
    forbid_resources,
    persona_join_arguments,
    printed_join_summary,
    write_persona,
)
from tests._memory_fakes import make_graphiti
from tests._runtime_fakes import SessionRun


@pytest.fixture
def memory_only_join(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[MagicMock], Coroutine[Any, Any, Any]]:
    settings.stt.api_key = SecretStr("muse-key")
    enable_audio_platform(monkeypatch)
    monkeypatch.setattr(providers, "MEMORY_CONNECT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(providers, "init_chat_model", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        cli_join, "run_meeting_session", lambda *_a, **_k: pytest.fail("browser must stay closed")
    )
    monkeypatch.setattr(
        assembly, "create_meeting_agent", lambda *_a, **_k: pytest.fail("agent must not be built")
    )
    arguments = persona_join_arguments(settings, memory=True, persona_path=write_persona(tmp_path))

    async def run(graphiti: MagicMock) -> Any:
        monkeypatch.setattr(
            memory_module, "create_graphiti_memory", lambda _config, **_kwargs: graphiti
        )
        return await join(MEET_LINK, **arguments)

    return run


@pytest.mark.parametrize(
    ("graphiti_options", "kind"),
    [
        ({"connect_error": ConnectionRefusedError("refused")}, "unknown"),
        ({"connect_gate": asyncio.Event()}, "timeout"),
    ],
)
async def test_join_memory_startup_failure_exits_before_browser(
    memory_only_join: Callable[[MagicMock], Coroutine[Any, Any, Any]],
    capsys: pytest.CaptureFixture[str],
    graphiti_options: dict[str, Any],
    kind: str,
) -> None:
    graphiti = make_graphiti(**graphiti_options)

    with pytest.raises(SystemExit) as caught:
        await memory_only_join(graphiti)

    assert caught.value.code == 3
    assert graphiti.events == ["verify", "close"]
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
        pytest.raises(FakeYouError, match="browser profile is in use: LeaseError") as caught,
    ):
        await join(MEET_LINK, profile_dir=str(profile))
    assert caught.value.code is ErrorCode.BROWSER_PROFILE_IN_USE
    assert isinstance(caught.value.__cause__, LeaseError)
    assert "lease is locked:" in str(caught.value.__cause__)


async def test_join_refuses_a_locked_meeting_record(
    join: Callable[..., Any], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.ingestion_status import record_lock

    forbid_resources(monkeypatch)
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-1"))

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
    [bound] = [row for row in rows if row["event"] == "signal_handlers_bound"]
    assert bound["signals"] == ["SIGINT", "SIGTERM"]
    assert bound["unavailable"] == []


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


@pytest.mark.asyncio
async def test_run_join_session_reports_the_outcome_through_its_ports(settings: Settings) -> None:
    configs: list[SessionConfig] = []

    async def run(config: SessionConfig, wiring: SessionWiring | None = None) -> SessionResult:
        del wiring
        configs.append(config)
        return SessionResult(phase="ended", terminal_reason="stopped_by_owner", detail="ok")

    def unused_memory(_settings: Settings) -> Any:
        pytest.fail("memory must stay closed without --memory")

    def unused_checkpointer(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("checkpointer must stay closed for a fresh meeting")

    outcome = await run_join_session(
        JoinRequest(
            link="https://meet.google.com/abc-defg-hij",
            settings=settings,
            identity=OperatorIdentity(None),
            audio_enabled=False,
            meeting_id="meeting-port",
        ),
        JoinPorts(run_session=run, open_memory=unused_memory),
    )

    assert outcome.exit_code is ExitCode.SUCCESS
    assert outcome.session.terminal_reason == "stopped_by_owner"
    assert [config.meeting_id for config in configs] == ["meeting-port"]
