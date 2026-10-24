import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId, is_identifier
from fakeyou.memory.persona import (
    PersonaProfile,
    load_persona,
    memory_group_for,
    persona_digest,
    require_persona_group,
)
from tests._persona_support import EXAMPLES, profile, profile_data

EXAMPLE_PROFILE = EXAMPLES / "persona.example.json"


EXAMPLE_PERSONA_DIGEST = "e9373b4317ab6a602c2d6a3ff0830bf18b8145bb1b9684c4a77b6fa968672f36"


def identity(owner_id: str = "owner-a", twin_id: str = "twin-a") -> dict[str, str]:
    return {"owner_id": owner_id, "twin_id": twin_id}


def write_profile(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "persona.json"
    path.write_text(content, encoding="utf-8")
    return path


async def test_example_profile_is_valid_and_synthetic() -> None:
    example = json.loads(EXAMPLE_PROFILE.read_text(encoding="utf-8"))

    loaded = await load_persona(
        EXAMPLE_PROFILE, **identity(owner_id=example["owner_id"], twin_id=example["twin_id"])
    )

    assert loaded.allowed_tools == frozenset({"recall_meeting_context"})
    assert loaded.ingest_transcripts is True
    assert loaded.verified_facts


async def test_load_persona_returns_validated_profile(tmp_path: Path) -> None:
    path = write_profile(tmp_path, json.dumps(profile_data()))

    loaded = await load_persona(path, **identity())

    assert loaded == profile()
    assert loaded.goals == ("Keep the weekly sync on schedule",)


async def test_load_persona_rejects_malformed_json(tmp_path: Path) -> None:
    path = write_profile(tmp_path, '{"owner_id": "owner-a",')

    with pytest.raises(ValidationError, match="json_invalid|Invalid JSON"):
        await load_persona(path, **identity())


@pytest.mark.parametrize(
    ("owner_id", "twin_id"), [("owner-b", "twin-a"), ("owner-a", "twin-b"), ("twin-a", "owner-a")]
)
async def test_load_persona_rejects_identity_mismatch(
    tmp_path: Path, owner_id: str, twin_id: str
) -> None:
    path = write_profile(tmp_path, json.dumps(profile_data()))

    with pytest.raises(FakeYouError, match="owner_id and twin_id") as caught:
        await load_persona(path, **identity(owner_id=owner_id, twin_id=twin_id))
    assert caught.value.code is ErrorCode.PERSONA_IDENTITY_MISMATCH


@pytest.mark.parametrize(
    "overrides",
    [
        {"unexpected": "field"},
        {"role": "   "},
        {"display_name": ""},
        {"speaking_style": " "},
        {"goals": ["valid goal", "  "]},
        {"verified_facts": [""]},
        {"verified_facts": ["fact"] * 21},
        {"role": "x" * 501},
        {"allowed_tools": ["remember_meeting_fact"]},
        {"allowed_tools": ["browser_click"]},
        {"language": "fr"},
        {"owner_id": "bad owner"},
        {"twin_id": "bad:twin"},
        {"owner_id": "../owner"},
        {"twin_id": "twin/../../x"},
        {"ingest_transcripts": "sometimes"},
        {"vocabulary": ["Orchid", "  "]},
        {"vocabulary": ["x" * 65]},
        {"vocabulary": [f"term{index}" for index in range(31)]},
    ],
)
def test_profile_rejects_invalid_fields(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        profile(**overrides)


def test_profile_errors_hide_input_values() -> None:
    with pytest.raises(ValidationError) as caught:
        profile(role="x" * 501, owner_id="private owner value")

    assert "private owner value" not in str(caught.value)
    assert "x" * 50 not in str(caught.value)


def test_profile_defaults_are_conservative() -> None:
    minimal = PersonaProfile.model_validate(
        {
            "owner_id": "owner-a",
            "twin_id": "twin-a",
            "display_name": "Rani Twin",
            "role": "Product lead",
        }
    )

    assert minimal.language == "id"
    assert minimal.speaking_style == "concise"
    assert minimal.goals == ()
    assert minimal.verified_facts == ()
    assert minimal.allowed_tools == frozenset()
    assert minimal.ingest_transcripts is False
    assert minimal.vocabulary == ()


def test_profile_is_immutable() -> None:
    loaded = profile()

    with pytest.raises(ValidationError):
        loaded.owner_id = SessionId("owner-b")
    assert isinstance(loaded.verified_facts, tuple)
    assert isinstance(loaded.allowed_tools, frozenset)


def test_memory_group_is_stable_across_rename_and_meetings() -> None:
    original = profile()
    renamed = profile(display_name="Rani Assistant", role="Different role")

    assert renamed.memory_group == original.memory_group
    assert is_identifier(original.memory_group)
    assert original.memory_group.startswith("twin_")
    assert "owner-a" not in original.memory_group


def test_memory_group_isolates_owners_and_twins() -> None:
    groups = {
        profile(owner_id=owner_id, twin_id=twin_id).memory_group
        for owner_id, twin_id in (
            ("owner-a", "twin-a"),
            ("owner-a", "twin-b"),
            ("owner-b", "twin-a"),
            ("owner-b", "twin-b"),
            ("twin-a", "owner-a"),
            ("owner_a", "twin"),
            ("owner", "a_twin"),
        )
    }

    assert len(groups) == 7


def test_memory_group_for_is_deterministic_scoped_and_matches_profile() -> None:
    group = memory_group_for("owner-a", "twin-a")

    assert group == memory_group_for("owner-a", "twin-a")
    assert group != memory_group_for("owner-a", "twin-b")
    assert group.startswith("twin_")
    assert group == profile(owner_id="owner-a", twin_id="twin-a").memory_group


def test_every_surface_derives_the_same_persona_group() -> None:
    built = profile(owner_id="owner-1", twin_id="twin-1")

    assert built.memory_group == memory_group_for("owner-1", "twin-1")
    assert (
        require_persona_group(built.memory_group, owner_id="owner-1", twin_id="twin-1")
        == built.memory_group
    )
    with pytest.raises(FakeYouError, match="does not match the ingestion status") as caught:
        require_persona_group("local_twin", owner_id="owner-1", twin_id="twin-1")
    assert caught.value.code is ErrorCode.MEMORY_INGESTION_GROUP_MISMATCH


def test_persona_digest_of_the_example_profile_is_pinned() -> None:
    loaded = PersonaProfile.model_validate_json(EXAMPLE_PROFILE.read_bytes())

    assert loaded.model_dump(mode="json")["allowed_tools"] == ["recall_meeting_context"]
    assert persona_digest(loaded) == EXAMPLE_PERSONA_DIGEST


async def test_persona_digest_ignores_file_formatting_and_key_order(tmp_path: Path) -> None:
    data = profile_data(owner_id="demo_owner", twin_id="demo_twin")
    compact = tmp_path / "compact.json"
    spaced = tmp_path / "spaced.json"
    compact.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    spaced.write_text(json.dumps(dict(reversed(data.items())), indent=4), encoding="utf-8")

    first = await load_persona(compact, owner_id="demo_owner", twin_id="demo_twin")
    second = await load_persona(spaced, owner_id="demo_owner", twin_id="demo_twin")

    assert persona_digest(first) == persona_digest(second)


@pytest.mark.parametrize(
    "overrides",
    [
        {"goals": ["Keep the weekly sync short"]},
        {"verified_facts": ["The Orchid beta launch is planned for 15 October 2026"]},
        {"allowed_tools": []},
        {"vocabulary": ["Orchid"]},
    ],
)
def test_persona_digest_changes_with_profile_content(overrides: dict[str, object]) -> None:
    assert persona_digest(profile(**overrides)) != persona_digest(profile())


def test_persona_digest_keeps_list_order() -> None:
    forward = profile(goals=["First goal", "Second goal"])
    backward = profile(goals=["Second goal", "First goal"])

    assert persona_digest(forward) != persona_digest(backward)
    assert persona_digest(forward) == persona_digest(profile(goals=["First goal", "Second goal"]))
