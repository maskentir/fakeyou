import re
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import BaseModel

from fakeyou.catalog.config import (
    API_KEY_OF_ROLE,
    COMPOSE_ONLY_KEYS,
    ENV_PREFIX,
    NESTED_DELIMITER,
    ConfigKey,
)
from fakeyou.config.settings import Settings


def _env_path(key: ConfigKey) -> tuple[str, ...]:
    return tuple(key.removeprefix(ENV_PREFIX).lower().split(NESTED_DELIMITER))


def _section_models(annotation: Any) -> list[Any]:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [model for member in get_args(annotation) for model in _section_models(member)]


def _resolves(path: tuple[str, ...]) -> bool:
    models: list[Any] = [Settings]
    for position, part in enumerate(path):
        fields = [model.model_fields[part] for model in models if part in model.model_fields]
        if not fields:
            return False
        if position == len(path) - 1:
            return True
        models = [member for field in fields for member in _section_models(field.annotation)]
        if not models:
            return False
    return True


@pytest.mark.parametrize(
    "key", sorted(set(ConfigKey) - COMPOSE_ONLY_KEYS), ids=lambda key: key.value
)
def test_settings_backed_keys_resolve_to_a_settings_field(key: ConfigKey) -> None:
    assert _resolves(_env_path(key))


@pytest.mark.parametrize("key", sorted(COMPOSE_ONLY_KEYS), ids=lambda key: key.value)
def test_compose_only_keys_do_not_map_to_settings(key: ConfigKey) -> None:
    assert not _resolves(_env_path(key))


@pytest.mark.parametrize("key", list(ConfigKey), ids=lambda key: key.value)
def test_config_keys_are_upper_snake_case_with_the_prefix(key: ConfigKey) -> None:
    assert key.value.startswith(ENV_PREFIX)
    assert key.value == key.value.upper()
    assert " " not in key.value


@pytest.mark.parametrize(
    ("role", "key"), sorted(API_KEY_OF_ROLE.items()), ids=lambda item: str(item)
)
def test_api_key_roles_resolve_to_their_settings_section(role: str, key: ConfigKey) -> None:
    assert _resolves((*role.split("."), "api_key"))


@pytest.mark.parametrize(
    ("role", "key"), sorted(API_KEY_OF_ROLE.items()), ids=lambda item: str(item)
)
def test_api_key_roles_match_the_environment_name_settings_derives(
    role: str, key: ConfigKey
) -> None:
    derived = (
        ENV_PREFIX + role.replace(".", NESTED_DELIMITER).upper() + NESTED_DELIMITER + "API_KEY"
    )

    assert key.value == derived


def _setting_env_keys(model: type[BaseModel], prefix: str = ENV_PREFIX) -> set[str]:
    keys: set[str] = set()
    for name, field in model.model_fields.items():
        members = _section_models(field.annotation)
        if members:
            for member in members:
                keys |= _setting_env_keys(member, f"{prefix}{name}{NESTED_DELIMITER}")
        else:
            keys.add(f"{prefix}{name}".upper())
    return keys


def test_env_example_documents_every_setting() -> None:
    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*(FAKEYOU_[A-Z0-9_]+)=", example, re.MULTILINE))

    assert _setting_env_keys(Settings) - documented == set()
