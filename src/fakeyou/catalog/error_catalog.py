import json
import sys
from typing import Any

from fakeyou.catalog.errors import CATALOG, ErrorCode
from fakeyou.catalog.exit_codes import TERMINAL_EXIT_CODES, ExitCode
from fakeyou.foundation.resources import read_package_text

PROSE: dict[str, Any] = json.loads(read_package_text("catalog", "error_catalog.json"))


def _cell(text: str) -> str:
    if "|" in text or "\n" in text:
        raise ValueError(f"table cell cannot contain a pipe or a newline: {text!r}")
    return text


def _code_span(text: str) -> str:
    _cell(text)
    if "`" in text:
        return f"`` {text} ``"
    return f"`{text}`"


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(_cell(cell) for cell in cells) + " |"


def _table(columns: list[str], rows: list[list[str]]) -> list[str]:
    return [_row(columns), _row(["---"] * len(columns)), *(_row(row) for row in rows)]


def render_error_catalog() -> str:
    lines: list[str] = [
        f"# {PROSE['title']}",
        "",
        PROSE["intro"],
        "",
        f"## {PROSE['rules_heading']}",
        "",
    ]
    lines += [f"- {rule}" for rule in PROSE["rules"]]
    lines += ["", f"## {PROSE['exit_heading']}", ""]
    lines += _table(
        PROSE["exit_columns"],
        [[str(code.value), code.name, PROSE["exit_meaning"][code.name]] for code in ExitCode],
    )
    lines += ["", f"## {PROSE['terminal_heading']}", "", PROSE["terminal_intro"], ""]
    lines += _table(
        PROSE["terminal_columns"],
        [[f"`{reason}`", exit_code.name] for reason, exit_code in TERMINAL_EXIT_CODES.items()],
    )
    domains: dict[str, list[ErrorCode]] = {}
    for code in ErrorCode:
        domains.setdefault(code.value.split(".")[0], []).append(code)
    lines += ["", f"## {PROSE['codes_heading']}", "", PROSE["codes_intro"]]
    for domain, codes in domains.items():
        lines += ["", f"### {domain}", ""]
        lines += _table(
            PROSE["codes_columns"],
            [
                [
                    _code_span(code.value),
                    _code_span(code.name),
                    CATALOG[code].exit_code.name,
                    _code_span(CATALOG[code].template),
                ]
                for code in codes
            ],
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render_error_catalog())
