from collections.abc import Iterator
from typing import Any

import pytest
import typer.main

from fakeyou.catalog.commands import Command
from fakeyou.cli.main import app


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
