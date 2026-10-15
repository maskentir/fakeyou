import json
import sys
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, SecretStr

from fakeyou.catalog.config import COMPOSE_ONLY_KEYS, ENV_PREFIX, NESTED_DELIMITER
from fakeyou.config.settings import Settings
from fakeyou.foundation.resources import read_package_text

PROSE: dict[str, Any] = json.loads(read_package_text("config", "env_catalog.json"))


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


def _section_models(annotation: Any) -> list[type[BaseModel]]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    models: list[type[BaseModel]] = []
    for member in get_args(annotation):
        models.extend(_section_models(member))
    return models


def _format_type(annotation: Any) -> str:
    if get_origin(annotation) is Union or isinstance(annotation, UnionType):
        return ", ".join(_format_type(member) for member in get_args(annotation))
    if hasattr(annotation, "__metadata__"):
        args = get_args(annotation)
        return _format_type(args[0]) if args else "Any"
    if isinstance(annotation, type):
        if annotation is type(None):
            return "None"
        return annotation.__name__
    name = str(annotation).replace("typing.", "")
    if name.startswith("Literal["):
        return name
    origin = get_origin(annotation)
    if origin is not None:
        inner = ", ".join(_format_type(member) for member in get_args(annotation))
        label = getattr(origin, "__name__", str(origin))
        return f"{label}[{inner}]"
    return name


def _format_default(field: Any) -> str:
    if field.is_required():
        return "wajib"
    if field.default_factory is not None:
        return "-"
    default = field.default
    if isinstance(default, SecretStr):
        return "`**********`" if default.get_secret_value() else "-"
    if default is None:
        return "-"
    if isinstance(default, bool):
        return str(default).lower()
    if isinstance(default, (str, int, float)):
        return _code_span(str(default)) if str(default) else "-"
    if isinstance(default, Path):
        return _code_span(str(default))
    try:
        return _code_span(json.dumps(default, default=str, ensure_ascii=False))
    except (TypeError, ValueError):
        return _code_span(repr(default))


def _setting_rows() -> dict[str, list[tuple[str, str, str]]]:
    sections: dict[str, dict[tuple[str, str, str, str | None], None]] = {}
    member_counts: dict[str, int] = {}
    for name, field in Settings.model_fields.items():
        members = _section_models(field.annotation)
        if members:
            member_counts[name] = len(members)
            for member in members:
                variant = member.__name__ if len(members) > 1 else None
                _collect_member(
                    member, f"{ENV_PREFIX}{name}{NESTED_DELIMITER}", name, variant, sections
                )
        else:
            rows = sections.setdefault(name, {})
            env_name = f"{ENV_PREFIX}{name}".upper()
            rows[(_format_type(field.annotation), _format_default(field), env_name, None)] = None
    merged: dict[str, list[tuple[str, str, str]]] = {}
    for section, rows in sections.items():
        by_name: dict[str, list[tuple[str, str, str | None]]] = {}
        for type_name, default, field_path, variant in rows:
            by_name.setdefault(field_path, []).append((type_name, default, variant))
        merged[section] = []
        for field_path in sorted(by_name):
            variants = by_name[field_path]
            first = (variants[0][0], variants[0][1])
            shared = len(variants) == member_counts.get(section, 1)
            if shared and all((type_name, default) == first for type_name, default, _ in variants):
                merged[section].append((field_path, first[0], first[1]))
            else:
                for type_name, default, variant in sorted(variants, key=str):
                    label = f"{field_path} ({variant})" if variant else field_path
                    merged[section].append((label, type_name, default))
    return merged


def _collect_member(
    model: type[BaseModel],
    prefix: str,
    section: str,
    variant: str | None,
    sections: dict[str, dict[tuple[str, str, str, str | None], None]],
) -> None:
    rows = sections.setdefault(section, {})
    for name, field in model.model_fields.items():
        members = _section_models(field.annotation)
        if members:
            for member in members:
                nested = member.__name__ if len(members) > 1 else variant
                _collect_member(
                    member, f"{prefix}{name}{NESTED_DELIMITER}", section, nested, sections
                )
        else:
            rows[
                (
                    _format_type(field.annotation),
                    _format_default(field),
                    f"{prefix}{name}".upper(),
                    variant,
                )
            ] = None


def render_env_catalog() -> str:
    lines: list[str] = [
        f"# {PROSE['title']}",
        "",
        PROSE["intro"],
        "",
        f"## {PROSE['rules_heading']}",
        "",
    ]
    lines += [f"- {rule}" for rule in PROSE["rules"]]
    lines += ["", f"## {PROSE['settings_heading']}", "", PROSE["settings_intro"]]
    for section, rows in _setting_rows().items():
        lines += ["", f"### {section}", ""]
        lines += _table(
            PROSE["settings_columns"],
            [
                [_code_span(name), _code_span(section), _code_span(type_name), default]
                for name, type_name, default in rows
            ],
        )
    lines += ["", f"## {PROSE['compose_heading']}", "", PROSE["compose_intro"], ""]
    lines += _table(
        PROSE["compose_columns"],
        [
            [_code_span(key.value), PROSE["compose_purpose"][key.value]]
            for key in sorted(COMPOSE_ONLY_KEYS)
        ],
    )
    lines += ["", f"## {PROSE['extra_heading']}", "", PROSE["extra_intro"], ""]
    lines += _table(
        PROSE["extra_columns"],
        [[_code_span(name), purpose, default] for name, purpose, default in PROSE["extra_rows"]],
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.stdout.write(render_env_catalog())
