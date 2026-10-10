import argparse
from collections.abc import Iterator

import pytest

from fakeyou.catalog.commands import Command
from fakeyou.cli.main import build_parser


def _command_paths(
    parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[str, ...]]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                yield (*prefix, name)
                yield from _command_paths(subparser, (*prefix, name))


PARSER_PATHS = frozenset(_command_paths(build_parser()))
FAKEYOU_COMMANDS = [command for command in Command if command.value.startswith("fakeyou ")]


@pytest.mark.parametrize("command", FAKEYOU_COMMANDS, ids=lambda command: command.value)
def test_fakeyou_commands_in_messages_exist_on_the_cli(command: Command) -> None:
    assert tuple(command.value.split()[1:]) in PARSER_PATHS
