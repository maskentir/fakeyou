import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import typer.main

from fakeyou.cli.main import app

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS = [ROOT / "README.md"]


def documented_command_lines() -> list[tuple[str, str]]:
    lines = []
    for document in DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        for block in re.findall(r"```sh\n(.*?)```", text, re.S):
            for line in block.replace("\\\n", " ").splitlines():
                if re.search(r"(^|\s)fakeyou\s", line):
                    lines.append((document.name, " ".join(line.split())))
    return lines


COMMANDS = documented_command_lines()


def _parse_only(tokens: list[str]) -> None:
    command: Any = typer.main.get_command(app)
    rest = list(tokens)
    while rest and getattr(command, "commands", None) and rest[0] in command.commands:
        command = command.commands[rest[0]]
        rest = rest[1:]
    command.make_context("fakeyou", rest)


def test_documentation_contains_commands() -> None:
    assert len(COMMANDS) >= 10


@pytest.mark.parametrize(("document", "line"), COMMANDS)
def test_documented_command_is_accepted_by_the_parser(document: str, line: str) -> None:
    del document
    tokens = shlex.split(line)
    if "fakeyou" not in tokens:
        pytest.skip("command builds or starts the worker without running fakeyou")
    _parse_only(tokens[tokens.index("fakeyou") + 1 :])
