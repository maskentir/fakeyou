import ast
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import (
    SessionIdField,
    parse_session_id,
    verify_chat_thread_id,
    verify_memory_group_id,
)
from fakeyou.debrief.debrief_schema import ObjectiveFinding

ROOT = Path(__file__).resolve().parent.parent
SOURCE_FILES = sorted((ROOT / "src" / "fakeyou").rglob("*.py"))

VALID = [
    "a",
    "A",
    "0",
    "m1",
    "meeting-1",
    "meeting_1",
    "a-b_c",
    "OPC-1",
    "local_owner",
    "1-2",
    "a_",
    "a-",
    "9" * 128,
    "a" * 128,
    "twin_6ba7b810-9dad-11d1-80b4-00c04fd430c8",
]
INVALID = [
    "",
    " ",
    "-a",
    "_a",
    "a b",
    "a/b",
    "../x",
    "..",
    ".",
    "a.b",
    "a:b",
    "a:w1",
    "a\n",
    "\na",
    "a\r",
    "a\tb",
    "  a",
    "a  ",
    "a" * 129,
    "é",
    "aé",
    "ａｂｃ",
    "a\x00b",
    "a/../b",
    "a\\b",
    "a;b",
    "$(x)",
    "`x`",
    "a'b",
    'a"b',
    "a%2Fb",
    "%2e%2e",
    "a b",
]


class _OwnerModel(BaseModel):
    owner_id: SessionIdField


@pytest.mark.parametrize("value", VALID)
def test_session_id_accepts_the_identifiers_it_accepted_before(value: str) -> None:
    assert parse_session_id(value, field="--meeting-id") == value


@pytest.mark.parametrize("value", INVALID)
def test_session_id_rejects_the_inputs_it_rejected_before(value: str) -> None:
    with pytest.raises(FakeYouError, match="--meeting-id") as caught:
        parse_session_id(value, field="--meeting-id")
    assert caught.value.code is ErrorCode.SESSION_ID_INVALID


@pytest.mark.parametrize("value", VALID)
def test_session_id_field_accepts_the_same_identifiers_in_models(value: str) -> None:
    assert _OwnerModel.model_validate({"owner_id": value}).owner_id == value


@pytest.mark.parametrize("value", INVALID)
def test_session_id_field_rejects_the_same_inputs_in_models(value: str) -> None:
    with pytest.raises(ValidationError, match="String should match pattern"):
        _OwnerModel.model_validate({"owner_id": value})


@pytest.mark.parametrize("value", VALID)
def test_memory_group_validator_accepts_identifiers(value: str) -> None:
    verify_memory_group_id(value)


@pytest.mark.parametrize("value", INVALID)
def test_memory_group_validator_rejects_with_its_catalog_code(value: str) -> None:
    with pytest.raises(FakeYouError, match="group_id") as caught:
        verify_memory_group_id(value)
    assert caught.value.code is ErrorCode.NOTE_GROUP_ID_INVALID


@pytest.mark.parametrize("value", VALID)
def test_chat_thread_validator_accepts_identifiers(value: str) -> None:
    verify_chat_thread_id(value)


@pytest.mark.parametrize("value", INVALID)
def test_chat_thread_validator_rejects_with_its_catalog_code(value: str) -> None:
    with pytest.raises(FakeYouError, match="--thread") as caught:
        verify_chat_thread_id(value)
    assert caught.value.code is ErrorCode.CHAT_THREAD_INVALID


@pytest.mark.parametrize("prefix", ["objective", "question"])
@pytest.mark.parametrize("value", VALID)
def test_objective_reference_accepts_prefixed_identifiers(prefix: str, value: str) -> None:
    finding = ObjectiveFinding.model_validate(
        {"objective_id": f"{prefix}:{value}", "status": "open", "evidence": ()}
    )
    assert finding.objective_id == f"{prefix}:{value}"


@pytest.mark.parametrize("prefix", ["objective", "question"])
@pytest.mark.parametrize("value", INVALID)
def test_objective_reference_rejects_the_same_bodies(prefix: str, value: str) -> None:
    with pytest.raises(ValidationError, match="String should match pattern"):
        ObjectiveFinding.model_validate(
            {"objective_id": f"{prefix}:{value}", "status": "open", "evidence": ()}
        )


def test_identifier_grammar_is_defined_once() -> None:
    this_file = Path(__file__).resolve()
    python_files = sorted(
        path for folder in ("src", "tests", "evals") for path in (ROOT / folder).rglob("*.py")
    )
    defining = [
        path.relative_to(ROOT)
        for path in python_files
        if path != this_file and "[A-Za-z0-9][A-Za-z0-9_-]" in path.read_text(encoding="utf-8")
    ]

    assert defining == [Path("src/fakeyou/config/identifiers.py")]


def test_session_id_is_constructed_only_by_the_parser() -> None:
    constructing = [
        path.relative_to(ROOT)
        for path in SOURCE_FILES
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SessionId"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]

    assert constructing == [Path("src/fakeyou/config/identifiers.py")]
