from __future__ import annotations

import os
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import psycopg
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.event_hooks import SerdeEvent, register_serde_event_listener
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

import fakeyou.db.checkpoint as checkpoint_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.conversation.participation_decision import ParticipationDecision
from fakeyou.db.checkpoint import checkpoint_conninfo, open_checkpointer
from tests._check_support import print_admin_check as check_setup
from tests._error_support import message_pattern
from tests._fakes_common import DATABASE_URL, env_file_opt


def _query_params(conninfo: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(conninfo).query))


def test_checkpoint_conninfo_carries_timeouts_and_keepalives() -> None:
    conninfo = checkpoint_conninfo(
        "postgresql://fakeyou:pw@db.internal:5432/fakeyou",
        connect_timeout_seconds=7.0,
        statement_timeout_seconds=9.0,
        keepalive_idle_seconds=11.0,
    )
    params = _query_params(conninfo)

    assert params["connect_timeout"] == "7"
    assert params["options"] == "-c statement_timeout=9000"
    assert params["keepalives"] == "1"
    assert params["keepalives_idle"] == "11"
    assert "-c%20statement_timeout%3D9000" in conninfo


def test_checkpoint_conninfo_floors_a_sub_millisecond_statement_timeout() -> None:
    conninfo = checkpoint_conninfo(
        "postgresql://fakeyou:pw@db.internal:5432/fakeyou",
        connect_timeout_seconds=7.0,
        statement_timeout_seconds=0.0004,
        keepalive_idle_seconds=11.0,
    )

    assert _query_params(conninfo)["options"] == "-c statement_timeout=1"


def test_checkpoint_conninfo_appends_to_existing_options() -> None:
    conninfo = checkpoint_conninfo(
        "postgresql://fakeyou:pw@db.internal:5432/fakeyou?options=-c%20search_path%3Dapp",
        connect_timeout_seconds=7.0,
        statement_timeout_seconds=9.0,
        keepalive_idle_seconds=11.0,
    )
    params = _query_params(conninfo)

    assert params["options"] == "-c search_path=app -c statement_timeout=9000"


def test_checkpoint_conninfo_rounds_subsecond_timeouts_up_to_one_second() -> None:
    conninfo = checkpoint_conninfo(
        "postgresql://fakeyou:pw@db.internal:5432/fakeyou",
        connect_timeout_seconds=0.5,
        statement_timeout_seconds=9.0,
        keepalive_idle_seconds=0.4,
    )
    params = _query_params(conninfo)

    assert params["connect_timeout"] == "1"
    assert params["keepalives_idle"] == "1"
    assert conninfo.startswith("postgresql://fakeyou:pw@db.internal:5432/fakeyou?")


def test_checkpoint_conninfo_preserves_existing_query_parameters() -> None:
    conninfo = checkpoint_conninfo(
        "postgresql://fakeyou:pw@db.internal:5432/fakeyou?sslmode=require",
        connect_timeout_seconds=7.0,
        statement_timeout_seconds=9.0,
        keepalive_idle_seconds=11.0,
    )
    params = _query_params(conninfo)

    assert params["sslmode"] == "require"
    assert params["connect_timeout"] == "7"


class _CapturingPool:
    seen: list[str] = []

    def __init__(self, conninfo: str, **_kwargs: Any) -> None:
        self.conninfo = conninfo
        _CapturingPool.seen.append(conninfo)

    async def __aenter__(self) -> _CapturingPool:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    @staticmethod
    async def check_connection(_connection: object) -> None:
        return None


class _SetupOnlySaver:
    def __init__(self, pool: object, *, serde: object = None) -> None:
        del pool, serde

    async def setup(self) -> None:
        return None


async def test_open_checkpointer_applies_timeouts_to_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _CapturingPool.seen.clear()
    monkeypatch.setattr(checkpoint_module, "AsyncConnectionPool", _CapturingPool)
    monkeypatch.setattr(checkpoint_module, "AsyncPostgresSaver", _SetupOnlySaver)

    async with open_checkpointer(
        "postgresql://fakeyou:pw@db.internal:5432/fakeyou",
        connect_timeout_seconds=7.0,
        statement_timeout_seconds=9.0,
        keepalive_idle_seconds=11.0,
    ):
        pass

    assert len(_CapturingPool.seen) == 1
    params = _query_params(_CapturingPool.seen[0])
    assert params["connect_timeout"] == "7"
    assert params["options"] == "-c statement_timeout=9000"


async def test_open_checkpointer_defaults_come_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _CapturingPool.seen.clear()
    monkeypatch.setattr(checkpoint_module, "AsyncConnectionPool", _CapturingPool)
    monkeypatch.setattr(checkpoint_module, "AsyncPostgresSaver", _SetupOnlySaver)

    async with open_checkpointer("postgresql://fakeyou:pw@db.internal:5432/fakeyou"):
        pass

    assert len(_CapturingPool.seen) == 1
    params = _query_params(_CapturingPool.seen[0])
    assert params["connect_timeout"] == "5"
    assert params["options"] == "-c statement_timeout=30000"
    assert params["keepalives"] == "1"


async def test_open_checkpointer_reports_an_unreachable_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkpoint_module, "AsyncConnectionPool", AsyncConnectionPool)
    monkeypatch.setattr(checkpoint_module, "AsyncPostgresSaver", AsyncPostgresSaver)

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.DATABASE_UNREACHABLE),
    ) as raised:
        async with open_checkpointer(
            "postgresql://fakeyou:secret@127.0.0.1:1/fakeyou", connect_timeout_seconds=0.5
        ):
            pass

    assert raised.value.code is ErrorCode.DATABASE_UNREACHABLE


async def test_open_checkpointer_maps_driver_errors_to_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingPool:
        def __init__(self, _conninfo: str, **_kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> FailingPool:
            raise psycopg.ProgrammingError("boom")

        async def __aexit__(self, *_exc: object) -> None:
            return None

        @staticmethod
        async def check_connection(_connection: object) -> None:
            return None

    monkeypatch.setattr(checkpoint_module, "AsyncConnectionPool", FailingPool)

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.DATABASE_UNREACHABLE),
    ) as raised:
        async with open_checkpointer("postgresql://localhost/db"):
            pass

    assert raised.value.code is ErrorCode.DATABASE_UNREACHABLE


async def test_checkpointer_restores_a_participation_decision_as_a_registered_type() -> None:
    decision = ParticipationDecision(decision="speak", confidence=0.9, reason_code="addressed")
    config: RunnableConfig = {"configurable": {"thread_id": "participation", "checkpoint_ns": ""}}
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"last_participation": decision}
    checkpoint["channel_versions"] = {"last_participation": 1}
    events: list[SerdeEvent] = []
    unregister = register_serde_event_listener(events.append)
    try:
        async with open_checkpointer(DATABASE_URL) as saver:
            await saver.aput(config, checkpoint, {"step": 1}, {"last_participation": 1})
            stored = await saver.aget_tuple(config)
    finally:
        unregister()

    assert stored is not None
    assert stored.checkpoint["channel_values"]["last_participation"] == decision
    assert events == []


def test_check_masks_the_database_url(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.database.url = SecretStr("postgresql://fakeyou:s3cret@localhost/fakeyou")

    check_setup(settings)

    report = capsys.readouterr().out
    assert "s3cret" not in report
    assert "**********" in report


def test_database_url_loads_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.upper().startswith("FAKEYOU_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("FAKEYOU_DATABASE__URL", "postgresql://fakeyou:x@localhost/fakeyou")

    url = Settings(**env_file_opt(None)).database.url

    assert url is not None
    assert url.get_secret_value() == "postgresql://fakeyou:x@localhost/fakeyou"


@pytest.mark.postgres
async def test_checkpointer_recovers_after_its_connection_is_terminated(
    migrated_database_url: str,
) -> None:
    config: RunnableConfig = {"configurable": {"thread_id": "reconnect", "checkpoint_ns": ""}}
    async with open_checkpointer(migrated_database_url) as saver:
        await saver.aput(config, empty_checkpoint(), {"step": 0}, {})
        async with await psycopg.AsyncConnection.connect(
            migrated_database_url, autocommit=True
        ) as admin:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        await saver.aput(config, empty_checkpoint(), {"step": 1}, {})
        stored = await saver.aget_tuple(config)

    assert stored is not None
    assert stored.metadata == {"step": 1}


@pytest.mark.postgres
async def test_checkpointer_state_survives_a_fresh_pool(migrated_database_url: str) -> None:
    config: RunnableConfig = {
        "configurable": {"thread_id": "reopen-fresh-pool", "checkpoint_ns": ""}
    }
    async with open_checkpointer(migrated_database_url) as saver:
        await saver.aput(config, empty_checkpoint(), {"step": 7}, {})

    async with open_checkpointer(migrated_database_url) as reopened:
        stored = await reopened.aget_tuple(config)

    assert stored is not None
    assert stored.metadata == {"step": 7}


@pytest.mark.container
async def test_checkpointer_fails_when_the_named_server_is_dead() -> None:
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.DATABASE_UNREACHABLE)
    ) as caught:
        async with open_checkpointer(
            "postgresql://fakeyou:harness-only@127.0.0.1:9/fakeyou",
            connect_timeout_seconds=1.0,
        ):
            pass

    assert caught.value.code is ErrorCode.DATABASE_UNREACHABLE
