import re
import shlex
from pathlib import Path

import pytest

from fakeyou.cli import build_parser

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


def documented_command_lines() -> list[tuple[str, str]]:
    lines = []
    for document in DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        for block in re.findall(r"```sh\n(.*?)```", text, re.S):
            for line in block.replace("\\\n", " ").splitlines():
                if re.search(r"(^|\s)fakeyou\s", line) or "meeting-worker" in line:
                    lines.append((document.name, " ".join(line.split())))
    return lines


COMMANDS = documented_command_lines()


def test_documentation_contains_commands() -> None:
    assert len(COMMANDS) >= 10


@pytest.mark.parametrize("document,line", COMMANDS)
def test_documented_command_is_accepted_by_the_parser(document: str, line: str) -> None:
    tokens = shlex.split(line)
    if "fakeyou" not in tokens:
        pytest.skip("command builds or starts the worker without running fakeyou")
    arguments = build_parser().parse_args(tokens[tokens.index("fakeyou") + 1 :])

    assert arguments.command


@pytest.mark.parametrize("document,line", COMMANDS)
def test_worker_commands_run_through_the_fakeyou_entry_point(document: str, line: str) -> None:
    tokens = shlex.split(line)
    if "run" not in tokens or "meeting-worker" not in tokens:
        pytest.skip("not a worker run command")

    assert tokens[tokens.index("meeting-worker") + 1] == "fakeyou"
