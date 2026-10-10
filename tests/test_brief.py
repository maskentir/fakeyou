import hashlib
import json
import re
from collections.abc import Callable
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.persistence.brief import (
    CONTEXT_FILE_NAME,
    MAX_BRIEF_BYTES,
    MAX_CONTEXT_BYTES,
    MeetingBrief,
    MeetingContext,
    brief_digest,
    context_path,
    load_meeting_brief,
    read_meeting_context,
    validate_brief_scope,
    write_meeting_context,
)

EXAMPLE_BRIEF = Path(__file__).resolve().parent.parent / "examples" / "meeting-brief.example.json"
SCOPE = {"owner_id": "demo_owner", "twin_id": "demo_twin", "meeting_id": "brief-demo-1003"}
PERSONA_SHA256 = hashlib.sha256(b"synthetic persona").hexdigest()
PRIVATE_MARKER = "PRIVATE-MARKER-7f3a"
EXAMPLE_DIGEST = "71c10c101633d82d64ececefd02618147b7a9e326e60e37345260457e67a7f23"


def test_context_snapshot_rejects_oversize_before_validation(tmp_path: Path) -> None:
    path = tmp_path / "meeting-context.json"
    path.write_bytes(b" " * (MAX_CONTEXT_BYTES + 1))
    with pytest.raises(FakeYouError, match="at most") as caught:
        read_meeting_context(path)
    assert caught.value.code is ErrorCode.MEETING_CONTEXT_TOO_LARGE


def brief_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(EXAMPLE_BRIEF.read_text(encoding="utf-8"))
    data.update(overrides)
    return data


def parse_brief(data: dict[str, Any]) -> MeetingBrief:
    return MeetingBrief.model_validate_json(json.dumps(data))


def items(count: int, prefix: str = "item") -> list[dict[str, str]]:
    return [
        {"id": f"{prefix}-{index}", "text": f"Synthetic text {index}."} for index in range(count)
    ]


def context_for(brief: MeetingBrief, persona_sha256: str = PERSONA_SHA256) -> MeetingContext:
    return MeetingContext(
        schema_version="meeting_context/v1",
        brief=brief,
        brief_sha256=brief_digest(brief),
        persona_sha256=persona_sha256,
    )


def write_brief_file(tmp_path: Path, content: bytes) -> Path:
    path = tmp_path / "brief.json"
    path.write_bytes(content)
    return path


@pytest.fixture
def brief() -> MeetingBrief:
    return MeetingBrief.model_validate_json(EXAMPLE_BRIEF.read_bytes())


def test_example_brief_loads_with_its_own_scope(brief: MeetingBrief) -> None:
    loaded = load_meeting_brief(EXAMPLE_BRIEF, **SCOPE)

    assert loaded == brief
    assert loaded.knowledge_scope == "brief_only"
    assert loaded.permissions == {"explain_facts", "discuss_options", "ask_brief_questions"}
    validate_brief_scope(brief, **SCOPE)


def test_meeting_scope_cannot_be_changed(brief: MeetingBrief) -> None:
    with pytest.raises(FakeYouError) as caught:
        validate_brief_scope(
            brief,
            owner_id=brief.owner_id,
            twin_id=brief.twin_id,
            meeting_id="another-meeting",
        )
    assert caught.value.code is ErrorCode.MEETING_BRIEF_SCOPE_MISMATCH


@pytest.mark.parametrize("field", ["owner_id", "twin_id", "meeting_id"])
def test_scope_mismatch_is_rejected_without_echoing_ids(brief: MeetingBrief, field: str) -> None:
    scope = {**SCOPE, field: "other-session"}

    with pytest.raises(FakeYouError) as error:
        validate_brief_scope(brief, **scope)
    assert error.value.code is ErrorCode.MEETING_BRIEF_SCOPE_MISMATCH

    assert SCOPE[field] not in str(error.value)
    assert "other-session" not in str(error.value)


@pytest.mark.parametrize("field", ["owner_id", "twin_id", "meeting_id"])
def test_load_rejects_a_brief_for_another_scope(tmp_path: Path, field: str) -> None:
    path = write_brief_file(tmp_path, EXAMPLE_BRIEF.read_bytes())

    with pytest.raises(FakeYouError) as caught:
        load_meeting_brief(path, **{**SCOPE, field: "other-session"})
    assert caught.value.code is ErrorCode.MEETING_BRIEF_SCOPE_MISMATCH


def test_load_propagates_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_meeting_brief(tmp_path / "absent.json", **SCOPE)


@pytest.mark.parametrize(
    "overrides",
    [
        {"permissions": ["explain_facts", "approve_budget"]},
        {"permissions": "explain_facts"},
        {"approve_budget": True},
        {"knowledge_scope": "everything"},
        {"objectives": [{"id": "blockers", "text": "Find blockers.", "approve_budget": True}]},
    ],
)
def test_unknown_permissions_and_fields_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_brief(brief_data(**overrides))


@pytest.mark.parametrize("collection", ["objectives", "approved_facts", "questions"])
def test_duplicate_item_ids_within_a_collection_are_rejected(collection: str) -> None:
    duplicated = [{"id": "same", "text": "First."}, {"id": "same", "text": "Second."}]

    with pytest.raises(ValidationError, match=f"{collection} item ids must be unique"):
        parse_brief(brief_data(**{collection: duplicated}))


def test_the_same_item_id_may_appear_in_different_collections() -> None:
    shared = [{"id": "shared", "text": "Shared synthetic text."}]

    parsed = parse_brief(brief_data(objectives=shared, approved_facts=shared, questions=shared))

    assert {parsed.objectives[0].id, parsed.approved_facts[0].id, parsed.questions[0].id} == {
        "shared"
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"agenda": [f"Agenda {index}" for index in range(21)]},
        {"objectives": items(21)},
        {"approved_facts": items(31)},
        {"questions": items(21)},
        {"agenda": ["a" * 501]},
        {"objectives": [{"id": "long", "text": "a" * 501}]},
        {"agenda": [""]},
        {"agenda": ["   "]},
        {"approved_facts": [{"id": "empty", "text": ""}]},
        {"questions": [{"id": "blank", "text": " \t\n "}]},
    ],
)
def test_collection_and_text_limits_are_enforced(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_brief(brief_data(**overrides))


def test_values_exactly_at_the_limits_are_accepted() -> None:
    parsed = parse_brief(
        brief_data(
            agenda=["a" * 500] * 20,
            objectives=items(20),
            approved_facts=items(29) + [{"id": "longest", "text": "b" * 500}],
            questions=items(20),
        )
    )

    assert (
        len(parsed.agenda),
        len(parsed.objectives),
        len(parsed.approved_facts),
        len(parsed.questions),
    ) == (20, 20, 30, 20)
    assert parsed.approved_facts[-1].text == "b" * 500


@pytest.mark.parametrize("session_id", ["", "has space", "-leading", "a" * 129, "dot.ted"])
@pytest.mark.parametrize("field", ["owner_id", "twin_id", "meeting_id", "item_id"])
def test_invalid_session_ids_are_rejected(field: str, session_id: str) -> None:
    if field == "item_id":
        data = brief_data(objectives=[{"id": session_id, "text": "Find blockers."}])
    else:
        data = brief_data(**{field: session_id})

    with pytest.raises(ValidationError):
        parse_brief(data)


@pytest.mark.parametrize(
    "overrides",
    [
        {"agenda": [123]},
        {"agenda": [True]},
        {"agenda": [1.5]},
        {"objectives": [{"id": 7, "text": "Find blockers."}]},
        {"objectives": [{"id": True, "text": "Find blockers."}]},
        {"approved_facts": [{"id": "fact", "text": 42}]},
        {"questions": [{"id": "question", "text": False}]},
        {"owner_id": 123},
        {"meeting_id": 1003},
        {"schema_version": "meeting_brief/v2"},
        {"schema_version": 1},
    ],
)
def test_values_are_not_coerced(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        parse_brief(brief_data(**overrides))


def test_schema_version_is_required() -> None:
    data = brief_data()
    del data["schema_version"]

    with pytest.raises(ValidationError):
        parse_brief(data)


def test_optional_fields_default_to_the_narrowest_brief() -> None:
    parsed = parse_brief({"schema_version": "meeting_brief/v1", **SCOPE})

    assert parsed.permissions == frozenset()
    assert parsed.knowledge_scope == "brief_only"
    assert (parsed.agenda, parsed.objectives, parsed.approved_facts, parsed.questions) == (
        (),
        (),
        (),
        (),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"agenda": [PRIVATE_MARKER * 40]},
        {"objectives": [{"id": f"{PRIVATE_MARKER} x", "text": "Find blockers."}]},
        {"approved_facts": [{"id": "fact", "text": PRIVATE_MARKER * 40}]},
        {"permissions": [PRIVATE_MARKER]},
        {"approve_budget": PRIVATE_MARKER},
        {"schema_version": PRIVATE_MARKER},
        {"knowledge_scope": PRIVATE_MARKER},
        {"objectives": [{"id": "same", "text": PRIVATE_MARKER}] * 2},
    ],
)
def test_validation_errors_hide_the_input(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as error:
        parse_brief(brief_data(**overrides))

    assert PRIVATE_MARKER not in str(error.value)


def test_load_errors_hide_the_input(tmp_path: Path) -> None:
    content = json.dumps(brief_data(agenda=[PRIVATE_MARKER * 40])).encode()
    path = write_brief_file(tmp_path, content)

    with pytest.raises(ValueError) as error:
        load_meeting_brief(path, **SCOPE)

    assert PRIVATE_MARKER not in str(error.value)


def padded_example(size: int) -> bytes:
    content = EXAMPLE_BRIEF.read_bytes()
    return content + b" " * (size - len(content))


def test_brief_of_exactly_the_byte_limit_is_accepted(tmp_path: Path, brief: MeetingBrief) -> None:
    path = write_brief_file(tmp_path, padded_example(MAX_BRIEF_BYTES))

    assert path.stat().st_size == MAX_BRIEF_BYTES
    assert load_meeting_brief(path, **SCOPE) == brief


@pytest.mark.parametrize(
    "content",
    [
        padded_example(MAX_BRIEF_BYTES + 1),
        b"{" * (MAX_BRIEF_BYTES + 1),
        b"x" * (4 * MAX_BRIEF_BYTES),
    ],
    ids=["valid-json", "invalid-json", "far-oversize"],
)
def test_oversize_brief_is_rejected_before_parsing(tmp_path: Path, content: bytes) -> None:
    path = write_brief_file(tmp_path, content)

    with pytest.raises(FakeYouError, match=str(MAX_BRIEF_BYTES)) as error:
        load_meeting_brief(path, **SCOPE)

    assert error.value.code is ErrorCode.MEETING_BRIEF_TOO_LARGE
    assert not isinstance(error.value, ValidationError)


def test_digest_ignores_key_order_permission_order_and_file_whitespace(
    tmp_path: Path, brief: MeetingBrief
) -> None:
    data = brief_data()
    reordered = dict(reversed(list(data.items())))
    reordered["permissions"] = list(reversed(data["permissions"]))
    variants = [
        json.dumps(data, separators=(",", ":")),
        json.dumps(reordered, indent=4),
        "\n\n" + json.dumps(data, indent=1) + "\n\t ",
    ]

    digests = {
        brief_digest(load_meeting_brief(write_brief_file(tmp_path, variant.encode()), **SCOPE))
        for variant in variants
    }

    assert digests == {brief_digest(brief)}
    assert re.fullmatch(r"[0-9a-f]{64}", brief_digest(brief))


@pytest.mark.parametrize(
    "overrides",
    [
        {"agenda": ["Synthetic kickoff agenda"]},
        {"objectives": [{"id": "blockers", "text": "Find every blocker."}]},
        {"approved_facts": [{"id": "demo-date", "text": "The demo moved to Monday."}]},
        {"questions": [{"id": "vendor-needs", "text": "Which vendor tasks remain open?"}]},
        {"permissions": ["explain_facts", "discuss_options"]},
        {"knowledge_scope": "persona_memory"},
        {"meeting_id": "brief-demo-1004"},
    ],
)
def test_digest_changes_when_the_brief_changes(
    brief: MeetingBrief, overrides: dict[str, Any]
) -> None:
    assert brief_digest(parse_brief(brief_data(**overrides))) != brief_digest(brief)


def test_digest_changes_when_only_an_item_id_changes(brief: MeetingBrief) -> None:
    data = brief_data()
    data["approved_facts"][0]["id"] = "renamed-fact"

    assert brief_digest(parse_brief(data)) != brief_digest(brief)


def test_context_path_sits_next_to_the_record(tmp_path: Path) -> None:
    record = tmp_path / "brief-demo-1003" / "record.jsonl"

    assert context_path(record) == tmp_path / "brief-demo-1003" / CONTEXT_FILE_NAME
    assert CONTEXT_FILE_NAME == "meeting-context.json"


def test_context_roundtrips_through_an_atomic_write(tmp_path: Path, brief: MeetingBrief) -> None:
    path = context_path(tmp_path / "brief-demo-1003" / "record.jsonl")
    context = context_for(brief)

    write_meeting_context(path, context)

    assert read_meeting_context(path) == context
    assert sorted(path.parent.iterdir()) == [path]


def test_context_roundtrips_non_ascii_text(tmp_path: Path) -> None:
    path = tmp_path / CONTEXT_FILE_NAME
    context = context_for(
        parse_brief(brief_data(approved_facts=[{"id": "venue", "text": "Café Zürich — 東京"}]))
    )

    write_meeting_context(path, context)

    assert read_meeting_context(path) == context
    assert "Café Zürich — 東京" in path.read_text(encoding="utf-8")


def test_context_bytes_do_not_depend_on_permission_order(tmp_path: Path) -> None:
    permission_orders = list(permutations(brief_data()["permissions"]))
    written = set()
    for index, order in enumerate(permission_orders):
        path = tmp_path / f"order-{index}" / CONTEXT_FILE_NAME
        write_meeting_context(path, context_for(parse_brief(brief_data(permissions=list(order)))))
        written.add(path.read_bytes())

    assert len(written) == 1
    stored = json.loads(written.pop())
    assert stored["brief"]["permissions"] == sorted(brief_data()["permissions"])


def test_writing_the_same_context_twice_yields_identical_bytes(
    tmp_path: Path, brief: MeetingBrief
) -> None:
    path = tmp_path / CONTEXT_FILE_NAME
    write_meeting_context(path, context_for(brief))
    first = path.read_bytes()

    write_meeting_context(path, context_for(brief))

    assert path.read_bytes() == first


def test_absent_context_reads_as_none(tmp_path: Path) -> None:
    assert read_meeting_context(tmp_path / CONTEXT_FILE_NAME) is None


def test_unreadable_context_propagates_os_errors(tmp_path: Path) -> None:
    path = tmp_path / CONTEXT_FILE_NAME
    path.mkdir()

    with pytest.raises(IsADirectoryError):
        read_meeting_context(path)


def truncated(content: bytes) -> bytes:
    return content[: len(content) // 2]


def with_changes(change: Callable[[dict[str, Any]], object]) -> Callable[[bytes], bytes]:
    def rewrite(content: bytes) -> bytes:
        payload = json.loads(content)
        change(payload)
        return json.dumps(payload).encode()

    return rewrite


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda content: b"{not json",
        lambda content: b"",
        truncated,
        with_changes(lambda payload: payload.update(schema_version="meeting_context/v2")),
        with_changes(lambda payload: payload.pop("schema_version")),
        with_changes(lambda payload: payload.update(extra=True)),
        with_changes(lambda payload: payload.pop("persona_sha256")),
        with_changes(lambda payload: payload.update(brief_sha256="0" * 64)),
        with_changes(
            lambda payload: payload["brief"]["approved_facts"][0].update(text="Changed fact.")
        ),
        with_changes(lambda payload: payload["brief"]["permissions"].pop()),
        with_changes(lambda payload: payload["brief"].update(knowledge_scope="persona_memory")),
        with_changes(lambda payload: payload.update(persona_sha256=PERSONA_SHA256.upper())),
        with_changes(lambda payload: payload.update(persona_sha256=PERSONA_SHA256[:63])),
    ],
    ids=[
        "invalid-json",
        "empty",
        "truncated",
        "wrong-schema-version",
        "missing-schema-version",
        "extra-field",
        "missing-persona-hash",
        "wrong-brief-hash",
        "tampered-fact",
        "tampered-permissions",
        "tampered-scope",
        "uppercase-persona-hash",
        "short-persona-hash",
    ],
)
def test_corrupt_or_tampered_context_is_rejected(
    tmp_path: Path, brief: MeetingBrief, corrupt: Callable[[bytes], bytes]
) -> None:
    path = tmp_path / CONTEXT_FILE_NAME
    write_meeting_context(path, context_for(brief))
    path.write_bytes(corrupt(path.read_bytes()))

    with pytest.raises(ValueError):
        read_meeting_context(path)


@pytest.mark.parametrize(
    "persona_sha256",
    [
        PERSONA_SHA256.upper(),
        PERSONA_SHA256[:32],
        PERSONA_SHA256 + "0",
        "g" * 64,
        PERSONA_SHA256 + "\n",
        PRIVATE_MARKER,
    ],
)
def test_context_rejects_invalid_persona_hashes(brief: MeetingBrief, persona_sha256: str) -> None:
    with pytest.raises(ValidationError) as error:
        context_for(brief, persona_sha256=persona_sha256)

    assert PRIVATE_MARKER not in str(error.value)


def test_context_rejects_a_hash_of_another_brief(brief: MeetingBrief) -> None:
    other = parse_brief(brief_data(questions=[]))

    with pytest.raises(ValidationError):
        MeetingContext(
            schema_version="meeting_context/v1",
            brief=brief,
            brief_sha256=brief_digest(other),
            persona_sha256=PERSONA_SHA256,
        )


def test_failed_first_context_write_leaves_no_snapshot_or_temp_file(
    tmp_path: Path, brief: MeetingBrief, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_replace(source: Path, target: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("fakeyou.persistence.ingestion_status.os.replace", reject_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_meeting_context(tmp_path / CONTEXT_FILE_NAME, context_for(brief))

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "changed",
    [
        lambda brief: context_for(brief, persona_sha256="b" * 64),
        lambda brief: context_for(parse_brief(brief_data(knowledge_scope="persona_memory"))),
    ],
    ids=["persona", "brief"],
)
def test_a_different_snapshot_for_the_same_meeting_is_refused(
    tmp_path: Path, brief: MeetingBrief, changed: Callable[[MeetingBrief], MeetingContext]
) -> None:
    path = tmp_path / CONTEXT_FILE_NAME
    previous = context_for(brief)
    write_meeting_context(path, previous)
    previous_bytes = path.read_bytes()

    with pytest.raises(FakeYouError, match="differs") as caught:
        write_meeting_context(path, changed(brief))
    assert caught.value.code is ErrorCode.MEETING_CONTEXT_DIFFERS

    assert path.read_bytes() == previous_bytes
    assert [child.name for child in tmp_path.iterdir()] == [CONTEXT_FILE_NAME]
    assert read_meeting_context(path) == previous


def test_rewriting_the_same_snapshot_does_not_touch_the_file(
    tmp_path: Path, brief: MeetingBrief, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / CONTEXT_FILE_NAME
    write_meeting_context(path, context_for(brief))
    first = path.read_bytes()

    def reject_replace(source: Path, target: Path) -> None:
        raise OSError("replace must not run")

    monkeypatch.setattr("fakeyou.persistence.ingestion_status.os.replace", reject_replace)
    write_meeting_context(path, context_for(load_meeting_brief(EXAMPLE_BRIEF, **SCOPE)))

    assert path.read_bytes() == first


def test_a_corrupt_snapshot_is_never_overwritten(tmp_path: Path, brief: MeetingBrief) -> None:
    path = tmp_path / CONTEXT_FILE_NAME
    path.write_bytes(b"{not json")

    with pytest.raises(ValueError):
        write_meeting_context(path, context_for(brief))

    assert path.read_bytes() == b"{not json"


def test_a_reformatted_brief_resumes_the_recorded_snapshot(
    tmp_path: Path, brief: MeetingBrief
) -> None:
    path = tmp_path / CONTEXT_FILE_NAME
    write_meeting_context(path, context_for(brief))
    data = brief_data()
    reordered = {key: data[key] for key in reversed(list(data))}
    reordered["permissions"] = list(reversed(data["permissions"]))
    reformatted = write_brief_file(tmp_path, json.dumps(reordered, indent=7).encode("utf-8"))
    changed = brief_data(approved_facts=[{"id": "demo-date", "text": "Changed fact."}])

    assert read_meeting_context(path) == context_for(load_meeting_brief(reformatted, **SCOPE))
    assert read_meeting_context(path) != context_for(parse_brief(changed))


def test_digest_of_the_example_brief_is_pinned(brief: MeetingBrief) -> None:
    assert brief_digest(brief) == EXAMPLE_DIGEST


def test_stored_context_lists_permissions_sorted(tmp_path: Path, brief: MeetingBrief) -> None:
    path = tmp_path / CONTEXT_FILE_NAME
    write_meeting_context(path, context_for(brief))

    stored = json.loads(path.read_bytes())

    assert stored["brief"]["permissions"] == [
        "ask_brief_questions",
        "discuss_options",
        "explain_facts",
    ]
    assert stored["brief_sha256"] == EXAMPLE_DIGEST


def test_briefs_and_contexts_are_frozen(brief: MeetingBrief) -> None:
    context = context_for(brief)

    with pytest.raises(ValidationError):
        brief.knowledge_scope = "persona_memory"
    with pytest.raises(ValidationError):
        context.brief_sha256 = "0" * 64


def test_load_reads_a_bounded_prefix_of_an_endless_file() -> None:
    with pytest.raises(FakeYouError, match="at most") as caught:
        load_meeting_brief(Path("/dev/zero"), **SCOPE)
    assert caught.value.code is ErrorCode.MEETING_BRIEF_TOO_LARGE


def test_contexts_for_another_brief_or_persona_are_not_equal(brief: MeetingBrief) -> None:
    original = context_for(brief)
    other_brief = parse_brief(brief_data(knowledge_scope="persona_memory"))

    assert context_for(brief) == original
    assert context_for(other_brief) != original
    assert context_for(brief, persona_sha256="b" * 64) != original
