import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, NamedTuple, get_args

import pytest
from pydantic import BaseModel

from fakeyou.catalog.config import (
    API_KEY_OF_ROLE,
    COMPOSE_ONLY_KEYS,
    ENV_PREFIX,
    NESTED_DELIMITER,
    PROVIDER_ROLES,
    ROLE_ATTRIBUTE_PATH,
    ROLE_HEADER_SUFFIX,
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


def test_compose_only_keys_are_only_true_compose_settings() -> None:
    assert {key.value for key in COMPOSE_ONLY_KEYS} == {
        "FAKEYOU_COMPOSE_WORKER_SESSIONS_MEM_LIMIT",
        "FAKEYOU_COMPOSE_WORKER_SESSIONS_CPUS",
        "FAKEYOU_COMPOSE_WORKER_INGESTION_MEM_LIMIT",
        "FAKEYOU_COMPOSE_WORKER_INGESTION_CPUS",
        "FAKEYOU_COMPOSE_WORKER_DEBRIEF_MEM_LIMIT",
        "FAKEYOU_COMPOSE_WORKER_DEBRIEF_CPUS",
        "FAKEYOU_COMPOSE_WORKER_REEMBED_MEM_LIMIT",
        "FAKEYOU_COMPOSE_WORKER_REEMBED_CPUS",
        "FAKEYOU_COMPOSE_EMBEDDINGS_IMAGE",
        "FAKEYOU_COMPOSE_EMBEDDINGS_MODEL_ID",
        "FAKEYOU_COMPOSE_EMBEDDINGS_REVISION",
        "FAKEYOU_COMPOSE_EMBEDDINGS_BASE_URL",
        "FAKEYOU_COMPOSE_NEO4J_URI",
    }
    assert not hasattr(ConfigKey, "COMPOSE_WORKER_DATABASE_URL")
    assert not hasattr(ConfigKey, "COMPOSE_WORKER_NEO4J_URI")
    assert not hasattr(ConfigKey, "COMPOSE_WORKER_EMBEDDING_BASE_URL")


def test_env_example_does_not_document_retired_compose_aliases() -> None:
    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")

    assert "FAKEYOU_COMPOSE_WORKER_DATABASE_URL" not in example
    assert "FAKEYOU_COMPOSE_WORKER_NEO4J_URI" not in example
    assert "FAKEYOU_COMPOSE_WORKER_EMBEDDING_BASE_URL" not in example


@pytest.mark.parametrize("key", list(ConfigKey), ids=lambda key: key.value)
def test_config_keys_are_upper_snake_case_with_the_prefix(key: ConfigKey) -> None:
    assert key.value.startswith(ENV_PREFIX)
    assert key.value == key.value.upper()
    assert " " not in key.value


@pytest.mark.parametrize(
    ("role", "key"), sorted(API_KEY_OF_ROLE.items()), ids=lambda item: str(item)
)
def test_api_key_roles_resolve_to_their_settings_section(role: str, key: ConfigKey) -> None:
    del key
    assert _resolves((*ROLE_ATTRIBUTE_PATH[role].split("."), "api_key"))


@pytest.mark.parametrize(
    ("role", "key"), sorted(API_KEY_OF_ROLE.items()), ids=lambda item: str(item)
)
def test_api_key_roles_match_the_environment_name_settings_derives(
    role: str, key: ConfigKey
) -> None:
    derived = (
        ENV_PREFIX
        + ROLE_ATTRIBUTE_PATH[role].replace(".", NESTED_DELIMITER).upper()
        + NESTED_DELIMITER
        + "API_KEY"
    )

    assert key.value == derived


def test_provider_roles_match_the_frozen_contract_list() -> None:
    assert PROVIDER_ROLES == (
        "reply",
        "reply_fallback",
        "reply_strong",
        "debrief",
        "stt",
        "tts",
        "tts_fallback",
        "memory_llm",
        "memory_embedder",
        "turn_judge",
        "perception",
    )
    assert set(API_KEY_OF_ROLE) == set(PROVIDER_ROLES)
    assert set(ROLE_ATTRIBUTE_PATH) == set(PROVIDER_ROLES)
    assert set(ROLE_HEADER_SUFFIX) == set(PROVIDER_ROLES)


def test_turn_judge_has_named_config_keys() -> None:
    assert ConfigKey.TURN_JUDGE_API_KEY.value == "FAKEYOU_TURN_JUDGE__MODEL__API_KEY"
    assert API_KEY_OF_ROLE["turn_judge"] is ConfigKey.TURN_JUDGE_API_KEY
    assert ROLE_HEADER_SUFFIX["turn_judge"] == "Turn-Judge"


def test_role_header_suffix_matches_the_frozen_map() -> None:
    assert ROLE_HEADER_SUFFIX == {
        "reply": "Reply",
        "reply_fallback": "Reply-Fallback",
        "reply_strong": "Reply-Strong",
        "debrief": "Debrief",
        "stt": "Stt",
        "tts": "Tts",
        "tts_fallback": "Tts-Fallback",
        "memory_llm": "Memory-Llm",
        "memory_embedder": "Memory-Embedder",
        "turn_judge": "Turn-Judge",
        "perception": "Perception",
    }


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


def test_env_example_documents_every_compose_only_key() -> None:
    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^#?\s*(FAKEYOU_[A-Z0-9_]+)=", example, re.MULTILINE))

    assert {key.value for key in COMPOSE_ONLY_KEYS} - documented == set()


def test_settings_leaf_count_matches_the_frozen_map() -> None:
    assert len(_setting_env_keys(Settings)) == 188


class _MigrationMap(NamedTuple):
    settings: dict[str, str]
    compose_only: dict[str, str]
    new_keys: tuple[str, ...]


EXPECTED_SETTINGS_COUNTS = {"keep": 188}
EXPECTED_COMPOSE_COUNTS = {"keep": 13, "delete": 5}
EXPECTED_NEW_COUNT = 0


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _value in pairs]

    assert sorted(keys) == sorted(set(keys))

    return dict(pairs)


def _load_migration_map() -> _MigrationMap:
    path = Path(__file__).resolve().parent / "fixtures" / "env_migration_d9.json"
    loaded: dict[str, Any] = json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_keys
    )

    assert set(loaded) == {"settings", "compose_only", "new"}
    settings: dict[str, str] = loaded["settings"]
    compose_only: dict[str, str] = loaded["compose_only"]
    new_keys: tuple[str, ...] = tuple(loaded["new"])

    return _MigrationMap(settings=settings, compose_only=compose_only, new_keys=new_keys)


def _assert_migration_map_matches_settings(migration_map: _MigrationMap) -> None:
    live = _setting_env_keys(Settings)
    new_keys = set(migration_map.new_keys)

    assert set(migration_map.settings) & set(migration_map.compose_only) == set()
    assert live - set(migration_map.settings) == set()
    assert set(migration_map.settings) - live == set()
    assert sorted(migration_map.new_keys) == sorted(new_keys)
    assert new_keys & set(migration_map.settings) == set()
    assert new_keys & set(migration_map.compose_only) == set()
    assert len(migration_map.settings) == sum(EXPECTED_SETTINGS_COUNTS.values())
    assert dict(Counter(migration_map.settings.values())) == EXPECTED_SETTINGS_COUNTS
    assert len(migration_map.new_keys) == EXPECTED_NEW_COUNT


def _assert_migration_map_matches_compose_only(migration_map: _MigrationMap) -> None:
    live_compose = {key.value for key in COMPOSE_ONLY_KEYS}
    kept = {key for key, decision in migration_map.compose_only.items() if decision == "keep"}
    deleted = {key for key, decision in migration_map.compose_only.items() if decision == "delete"}

    assert kept | deleted == set(migration_map.compose_only)
    assert live_compose == kept
    assert deleted & live_compose == set()
    assert len(migration_map.compose_only) == sum(EXPECTED_COMPOSE_COUNTS.values())
    assert dict(Counter(migration_map.compose_only.values())) == EXPECTED_COMPOSE_COUNTS


def test_migration_map_covers_every_current_setting_once() -> None:
    _assert_migration_map_matches_settings(_load_migration_map())


def test_migration_map_covers_every_compose_only_key() -> None:
    _assert_migration_map_matches_compose_only(_load_migration_map())


def test_compose_only_keys_cover_every_compose_variable() -> None:
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    referenced = set(re.findall(r"FAKEYOU_COMPOSE_[A-Z0-9_]+", compose))
    cataloged = {key.value for key in COMPOSE_ONLY_KEYS}

    assert referenced - cataloged == set()


def test_compose_settings_variables_resolve_to_a_settings_field() -> None:
    compose = (Path(__file__).resolve().parent.parent / "compose.yaml").read_text(encoding="utf-8")
    referenced = {
        key
        for key in re.findall(r"FAKEYOU_[A-Z0-9_]+", compose)
        if not key.startswith("FAKEYOU_COMPOSE_")
    }
    unresolved = {
        key
        for key in referenced
        if not _resolves(tuple(key.removeprefix(ENV_PREFIX).lower().split(NESTED_DELIMITER)))
    }

    assert unresolved == set()
