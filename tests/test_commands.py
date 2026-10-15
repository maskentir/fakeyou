import shutil
from collections.abc import Iterator
from typing import Any

import pytest
import typer.main

from fakeyou.catalog.binaries import resolve_docker_binary, resolve_node_binary
from fakeyou.catalog.commands import Command
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli.main import app
from tests._error_support import message_pattern


def _command_paths(command: Any, prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    subcommands = getattr(command, "commands", None)
    if subcommands is None:
        return
    for name, subcommand in subcommands.items():
        yield (*prefix, name)
        yield from _command_paths(subcommand, (*prefix, name))


PARSER_PATHS = frozenset(_command_paths(typer.main.get_command(app)))
FAKEYOU_COMMANDS = [command for command in Command if command.value.startswith("fakeyou ")]


@pytest.mark.parametrize("command", FAKEYOU_COMMANDS, ids=lambda command: command.value)
def test_fakeyou_commands_in_messages_exist_on_the_cli(command: Command) -> None:
    assert tuple(command.value.split()[1:]) in PARSER_PATHS


def test_resolve_binaries_return_absolute_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    assert resolve_node_binary() == "/usr/bin/node"
    assert resolve_docker_binary() == "/usr/bin/docker"


def test_resolve_binaries_raise_catalog_errors_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.BROWSER_NODE_MISSING)
    ) as node_error:
        resolve_node_binary()
    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.DOCKER_UNAVAILABLE)
    ) as docker_error:
        resolve_docker_binary()

    assert node_error.value.code is ErrorCode.BROWSER_NODE_MISSING
    assert docker_error.value.code is ErrorCode.DOCKER_UNAVAILABLE
    assert "PATH" in str(docker_error.value)
