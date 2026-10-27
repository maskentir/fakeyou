import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from fakeyou.foundation.provenance import FactEvidence, SourceEvidence, render_evidence


def test_identical_facts_keep_both_source_times() -> None:
    older = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    newer = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    recorded = datetime(2026, 10, 2, 8, tzinfo=UTC)
    meeting = SourceEvidence(
        "e1", "meeting_transcript", "Google Meet m1 transcript", older, older, "episode_window"
    )
    owner = SourceEvidence("e2", "owner_note", "owner note", newer, recorded, "document")
    facts = [
        FactEvidence(("a",), "Orchid demo moves to Friday.", (meeting,)),
        FactEvidence(("b",), "Orchid demo  moves to Friday.", (owner,)),
    ]
    payload = json.loads(render_evidence(facts, result_limit=1, excluded={}))
    assert payload["schema"] == "memory_evidence/v1"
    assert len(payload["facts"]) == 1
    assert payload["facts"][0]["fact_ids"] == ["a", "b"]
    assert payload["facts"][0]["text"] == "Orchid demo moves to Friday."
    assert {item["source_at"] for item in payload["facts"][0]["sources"]} == {
        older.isoformat(),
        newer.isoformat(),
    }
    assert payload["facts"][0]["sources"][0]["episode_id"] == "e2"
    assert payload["excluded"] == {"pending": 0, "superseded": 0, "missing_source": 0}


def test_rendered_sources_state_speaker_identity_explicitly() -> None:
    at = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    meeting = SourceEvidence(
        "e1",
        "meeting_transcript",
        "Google Meet m1 transcript",
        at,
        at,
        "episode_window",
        speaker_identity="unverified: may or may not be the owner or any named person",
    )
    owner = SourceEvidence(
        "e2", "owner_note", "owner note", at, at, "document", speaker_identity="owner"
    )
    facts = [
        FactEvidence(("a",), "Orchid demo moves to Friday.", (meeting, owner)),
    ]
    payload = json.loads(render_evidence(facts, result_limit=5, excluded={}))
    sources = payload["facts"][0]["sources"]
    assert sources[0]["speaker_identity"] == "owner"
    assert sources[1]["speaker_identity"] == (
        "unverified: may or may not be the owner or any named person"
    )


def test_rendered_sources_default_to_unverified_speaker_identity() -> None:
    at = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    meeting = SourceEvidence(
        "e1", "meeting_transcript", "Google Meet m1 transcript", at, at, "episode_window"
    )
    facts = [FactEvidence(("a",), "Orchid demo moves to Friday.", (meeting,))]
    payload = json.loads(render_evidence(facts, result_limit=5, excluded={}))
    assert payload["facts"][0]["sources"][0]["speaker_identity"] == (
        "unverified: may or may not be the owner or any named person"
    )


def test_compact_render_drops_metadata_but_keeps_identity_longhand() -> None:
    at = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    recorded = datetime(2026, 10, 2, 8, tzinfo=UTC)
    meeting = SourceEvidence(
        "e1", "meeting_transcript", "Google Meet m1 transcript", at, at, "episode_window"
    )
    owner = SourceEvidence(
        "e2", "owner_note", "owner note", at, recorded, "document", speaker_identity="owner"
    )
    facts = [FactEvidence(("a",), "Orchid demo moves to Friday.", (meeting, owner))]
    payload = json.loads(render_evidence(facts, result_limit=5, excluded={}, compact=True))
    assert payload["schema"] == "memory_evidence/v1"
    assert payload["facts"][0]["text"] == "Orchid demo moves to Friday."
    assert payload["facts"][0]["fact_ids"] == ["a"]
    sources = payload["facts"][0]["sources"]
    assert sources[0] == {
        "kind": "owner_note",
        "label": "owner note",
        "source_at": at.astimezone(UTC).isoformat(),
        "recorded_at": recorded.astimezone(UTC).isoformat(),
        "speaker_identity": "owner",
    }
    assert sources[1] == {
        "kind": "meeting_transcript",
        "label": "Google Meet m1 transcript",
        "source_at": at.astimezone(UTC).isoformat(),
        "speaker_identity": "unverified: may or may not be the owner or any named person",
    }


def test_negation_stays_split() -> None:
    base = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    recorded = datetime(2026, 10, 2, 8, tzinfo=UTC)
    first = SourceEvidence("e1", "owner_note", "owner note", base, recorded, "document")
    second = SourceEvidence("e2", "owner_note", "owner note", base, recorded, "document")
    facts = [
        FactEvidence(("a",), "Orchid demo moves to Friday.", (first,)),
        FactEvidence(("b",), "Orchid demo does not move to Friday.", (second,)),
    ]
    payload = json.loads(render_evidence(facts, result_limit=5, excluded={}))
    assert len(payload["facts"]) == 2
    assert {item["text"] for item in payload["facts"]} == {
        "Orchid demo moves to Friday.",
        "Orchid demo does not move to Friday.",
    }


def test_conflicting_metadata_within_merge_fails() -> None:
    base = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    recorded = datetime(2026, 10, 2, 8, tzinfo=UTC)
    first = SourceEvidence("e1", "owner_note", "owner note", base, recorded, "document")
    second = SourceEvidence(
        "e1", "meeting_transcript", "Google Meet m1 transcript", base, base, "episode_window"
    )
    facts = [
        FactEvidence(("a",), "Orchid demo moves to Friday.", (first,)),
        FactEvidence(("b",), "Orchid demo moves to Friday.", (second,)),
    ]
    with pytest.raises(ValueError, match="e1"):
        render_evidence(facts, result_limit=5, excluded={})


def test_conflicting_metadata_across_facts_fails() -> None:
    base = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    recorded = datetime(2026, 10, 2, 8, tzinfo=UTC)
    first = SourceEvidence("e1", "owner_note", "owner note", base, recorded, "document")
    second = SourceEvidence(
        "e1", "meeting_transcript", "Google Meet m1 transcript", base, base, "episode_window"
    )
    facts = [
        FactEvidence(("a",), "Orchid demo moves to Friday.", (first,)),
        FactEvidence(("b",), "Budget is final.", (second,)),
    ]
    with pytest.raises(ValueError, match="e1"):
        render_evidence(facts, result_limit=5, excluded={})


def test_reversed_input_renders_same_payload() -> None:
    older = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    newer = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    recorded = datetime(2026, 10, 2, 8, tzinfo=UTC)
    first = SourceEvidence("e1", "owner_note", "owner note", older, recorded, "document")
    second = SourceEvidence("e2", "owner_note", "owner note", newer, recorded, "document")
    third = SourceEvidence(
        "e3", "meeting_transcript", "Google Meet m1 transcript", newer, newer, "episode_window"
    )
    facts = [
        FactEvidence(("a",), "Orchid demo moves to Friday.", (first,)),
        FactEvidence(("b",), "Budget is final.", (second,)),
        FactEvidence(("c",), "Venue is Hall A.", (third,)),
    ]
    forward = render_evidence(facts, result_limit=5, excluded={})
    backward = render_evidence(list(reversed(facts)), result_limit=5, excluded={})
    assert forward == backward
    assert json.loads(forward) == json.loads(backward)


def test_empty_fact_list_renders_empty_payload() -> None:
    payload = json.loads(render_evidence([], result_limit=5, excluded={}))
    assert payload["schema"] == "memory_evidence/v1"
    assert payload["facts"] == []
    assert payload["excluded"] == {"pending": 0, "superseded": 0, "missing_source": 0}


def test_fact_without_sources_renders_empty_sources() -> None:
    payload = json.loads(
        render_evidence(
            [FactEvidence(("a",), "Orchid demo moves to Friday.", ())],
            result_limit=5,
            excluded={},
        )
    )
    assert len(payload["facts"]) == 1
    assert payload["facts"][0]["fact_ids"] == ["a"]
    assert payload["facts"][0]["sources"] == []


def test_naive_datetimes_fail() -> None:
    aware = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    naive = datetime(2026, 9, 30, 3, 10)
    with pytest.raises(ValueError, match="timezone"):
        SourceEvidence("e1", "owner_note", "owner note", naive, aware, "document")
    with pytest.raises(ValueError, match="timezone"):
        SourceEvidence("e1", "owner_note", "owner note", aware, naive, "document")


def test_result_limit_below_one_fails() -> None:
    base = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    source = SourceEvidence("e1", "owner_note", "owner note", base, base, "document")
    fact = FactEvidence(("a",), "Orchid demo moves to Friday.", (source,))
    with pytest.raises(ValueError, match="result_limit"):
        render_evidence([fact], result_limit=0, excluded={})
    with pytest.raises(ValueError, match="result_limit"):
        render_evidence([fact], result_limit=-1, excluded={})


def test_excluded_missing_keys_default_to_zero() -> None:
    payload = json.loads(render_evidence([], result_limit=5, excluded={}))
    assert payload["excluded"] == {"pending": 0, "superseded": 0, "missing_source": 0}
    partial = json.loads(render_evidence([], result_limit=5, excluded={"pending": 2}))
    assert partial["excluded"] == {"pending": 2, "superseded": 0, "missing_source": 0}


def test_excluded_values_preserved() -> None:
    payload = json.loads(
        render_evidence(
            [], result_limit=5, excluded={"pending": 2, "superseded": 1, "missing_source": 3}
        )
    )
    assert payload["excluded"] == {"pending": 2, "superseded": 1, "missing_source": 3}


def test_excluded_unknown_key_fails() -> None:
    with pytest.raises(ValueError, match="excluded"):
        render_evidence([], result_limit=5, excluded={"bogus": 1})


def test_excluded_negative_fails() -> None:
    with pytest.raises(ValueError, match="excluded"):
        render_evidence([], result_limit=5, excluded={"pending": -1})


def test_owner_note_sorts_first_then_newest() -> None:
    older = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    newer = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    recorded = datetime(2026, 10, 2, 8, tzinfo=UTC)
    owner_old = SourceEvidence("e1", "owner_note", "owner note", older, recorded, "document")
    owner_new = SourceEvidence("e2", "owner_note", "owner note", newer, recorded, "document")
    talk_new = SourceEvidence(
        "e3", "meeting_transcript", "Google Meet m1 transcript", newer, newer, "episode_window"
    )
    facts = [
        FactEvidence(("a",), "Venue is Hall A.", (talk_new,)),
        FactEvidence(("b",), "Budget is final.", (owner_old,)),
        FactEvidence(("c",), "Orchid demo moves to Friday.", (owner_new,)),
    ]
    payload = json.loads(render_evidence(facts, result_limit=5, excluded={}))
    assert [item["text"] for item in payload["facts"]] == [
        "Orchid demo moves to Friday.",
        "Budget is final.",
        "Venue is Hall A.",
    ]


def test_fact_id_breaks_ties() -> None:
    base = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    first = SourceEvidence(
        "e1", "meeting_transcript", "Google Meet m1 transcript", base, base, "episode_window"
    )
    second = SourceEvidence(
        "e2", "meeting_transcript", "Google Meet m1 transcript", base, base, "episode_window"
    )
    facts = [
        FactEvidence(("b",), "Budget is final.", (second,)),
        FactEvidence(("a",), "Orchid demo moves to Friday.", (first,)),
    ]
    payload = json.loads(render_evidence(facts, result_limit=5, excluded={}))
    assert [item["fact_ids"] for item in payload["facts"]] == [["a"], ["b"]]


def test_limit_applies_after_merge() -> None:
    old = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    new = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    recorded = datetime(2026, 10, 2, 8, tzinfo=UTC)
    owner = SourceEvidence("e1", "owner_note", "owner note", new, recorded, "document")
    talk_old = SourceEvidence(
        "e2", "meeting_transcript", "Google Meet m1 transcript", old, old, "episode_window"
    )
    talk_other = SourceEvidence(
        "e3", "meeting_transcript", "Google Meet m1 transcript", old, old, "episode_window"
    )
    facts = [
        FactEvidence(("a",), "Orchid demo moves to Friday.", (owner,)),
        FactEvidence(("b",), "Orchid demo  moves to Friday.", (talk_old,)),
        FactEvidence(("c",), "Budget is final.", (talk_other,)),
    ]
    payload = json.loads(render_evidence(facts, result_limit=2, excluded={}))
    assert len(payload["facts"]) == 2
    assert payload["facts"][0]["fact_ids"] == ["a", "b"]
    assert payload["facts"][1]["fact_ids"] == ["c"]
    single = json.loads(render_evidence(facts, result_limit=1, excluded={}))
    assert len(single["facts"]) == 1
    assert single["facts"][0]["fact_ids"] == ["a", "b"]


def test_sources_order_owner_first_then_newest() -> None:
    old = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    mid = datetime(2026, 9, 30, 5, 0, tzinfo=UTC)
    new = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    recorded = datetime(2026, 10, 2, 8, tzinfo=UTC)
    owner_old = SourceEvidence("e2", "owner_note", "owner note", old, recorded, "document")
    talk_new = SourceEvidence(
        "e1", "meeting_transcript", "Google Meet m1 transcript", new, new, "episode_window"
    )
    talk_mid = SourceEvidence(
        "e3", "meeting_transcript", "Google Meet m1 transcript", mid, mid, "episode_window"
    )
    facts = [
        FactEvidence(("a",), "Orchid demo moves to Friday.", (talk_new,)),
        FactEvidence(("b",), "Orchid demo moves to Friday.", (owner_old,)),
        FactEvidence(("c",), "Orchid demo moves to Friday.", (talk_mid,)),
    ]
    payload = json.loads(render_evidence(facts, result_limit=5, excluded={}))
    assert len(payload["facts"]) == 1
    assert payload["facts"][0]["fact_ids"] == ["a", "b", "c"]
    assert [item["episode_id"] for item in payload["facts"][0]["sources"]] == ["e2", "e1", "e3"]


def test_timestamps_normalize_to_utc() -> None:
    zone = timezone(timedelta(hours=7))
    source_at = datetime(2026, 10, 1, 0, 30, tzinfo=zone)
    recorded_at = datetime(2026, 10, 2, 8, tzinfo=UTC)
    source = SourceEvidence(
        "e1",
        "meeting_transcript",
        "Google Meet m1 transcript",
        source_at,
        recorded_at,
        "episode_window",
    )
    payload = json.loads(
        render_evidence(
            [FactEvidence(("a",), "Orchid demo moves to Friday.", (source,))],
            result_limit=5,
            excluded={},
        )
    )
    assert payload["facts"][0]["sources"][0]["source_at"] == "2026-09-30T17:30:00+00:00"
    assert payload["facts"][0]["sources"][0]["recorded_at"] == recorded_at.isoformat()


def test_duplicate_source_with_same_metadata_dedupes() -> None:
    base = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    recorded = datetime(2026, 10, 2, 8, tzinfo=UTC)
    first = SourceEvidence("e1", "owner_note", "owner note", base, recorded, "document")
    second = SourceEvidence("e1", "owner_note", "owner note", base, recorded, "document")
    facts = [
        FactEvidence(("a",), "Orchid demo moves to Friday.", (first,)),
        FactEvidence(("b",), "Orchid demo moves to Friday.", (second,)),
    ]
    payload = json.loads(render_evidence(facts, result_limit=5, excluded={}))
    assert len(payload["facts"]) == 1
    assert payload["facts"][0]["fact_ids"] == ["a", "b"]
    assert len(payload["facts"][0]["sources"]) == 1
    assert payload["facts"][0]["sources"][0]["episode_id"] == "e1"


def test_blank_text_fails() -> None:
    base = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    source = SourceEvidence("e1", "owner_note", "owner note", base, base, "document")
    with pytest.raises(ValueError, match="blank"):
        FactEvidence(("a",), "   ", (source,))
    with pytest.raises(ValueError, match="blank"):
        FactEvidence(("a",), "", (source,))


def test_empty_fact_ids_fails() -> None:
    base = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    source = SourceEvidence("e1", "owner_note", "owner note", base, base, "document")
    with pytest.raises(ValueError, match="fact_ids"):
        FactEvidence((), "Orchid demo moves to Friday.", (source,))
    with pytest.raises(ValueError, match="fact_ids"):
        FactEvidence(("",), "Orchid demo moves to Friday.", (source,))


def test_empty_episode_id_fails() -> None:
    base = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="episode_id"):
        SourceEvidence("", "owner_note", "owner note", base, base, "document")
    with pytest.raises(ValueError, match="episode_id"):
        SourceEvidence("   ", "owner_note", "owner note", base, base, "document")


def test_invalid_kind_and_granularity_fail() -> None:
    base = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="kind"):
        SourceEvidence("e1", cast(Any, "bogus"), "owner note", base, base, "document")
    with pytest.raises(ValueError, match="time_granularity"):
        SourceEvidence("e1", "owner_note", "owner note", base, base, cast(Any, "bogus"))


def test_whitespace_variants_merge() -> None:
    base = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    first = SourceEvidence("e1", "owner_note", "owner note", base, base, "document")
    second = SourceEvidence("e2", "owner_note", "owner note", base, base, "document")
    facts = [
        FactEvidence(("a",), "  Orchid demo moves to Friday.  ", (first,)),
        FactEvidence(("b",), "Orchid demo moves to Friday.", (second,)),
    ]
    payload = json.loads(render_evidence(facts, result_limit=5, excluded={}))
    assert len(payload["facts"]) == 1
    assert payload["facts"][0]["text"] == "Orchid demo moves to Friday."
    assert payload["facts"][0]["fact_ids"] == ["a", "b"]


def test_rendered_json_roundtrips_through_file(tmp_path: Path) -> None:
    base = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    recorded = datetime(2026, 10, 2, 8, tzinfo=UTC)
    source = SourceEvidence("e1", "owner_note", "owner note", base, recorded, "document")
    rendered = render_evidence(
        [FactEvidence(("a",), "Orchid demo moves to Friday.", (source,))],
        result_limit=5,
        excluded={"pending": 1},
    )
    target = tmp_path / "evidence.json"
    target.write_text(rendered, encoding="utf-8")
    reloaded = json.loads(target.read_text(encoding="utf-8"))
    assert json.loads(rendered) == reloaded
    assert reloaded["excluded"] == {"pending": 1, "superseded": 0, "missing_source": 0}


def test_screen_and_chat_sources_render_as_episode_windows_under_one_schema() -> None:
    at = datetime(2026, 10, 3, 12, tzinfo=UTC)
    screen = SourceEvidence(
        "e8", "meeting_screen", "Google Meet m1 shared screen", at, at, "episode_window"
    )
    chat = SourceEvidence("e9", "meeting_chat", "Google Meet m1 chat", at, at, "episode_window")
    facts = [
        FactEvidence(("s1",), "Quarterly revenue grew.", (screen,)),
        FactEvidence(("c1",), "Send the deck after the call.", (chat,)),
    ]
    payload = json.loads(render_evidence(facts, result_limit=5, excluded={}))

    assert payload["schema"] == "memory_evidence/v1"
    sources = [fact["sources"][0] for fact in payload["facts"]]
    assert {source["kind"] for source in sources} == {"meeting_screen", "meeting_chat"}
    assert {source["time_granularity"] for source in sources} == {"episode_window"}
    assert all("frame_ref" not in source for source in sources)
    slim = json.loads(render_evidence(facts, result_limit=5, excluded={}, compact=True))
    assert slim["schema"] == "memory_evidence/v1"


def test_removed_visual_kind_and_frame_granularity_are_rejected() -> None:
    at = datetime(2026, 10, 3, 12, tzinfo=UTC)
    with pytest.raises(ValueError, match="kind"):
        SourceEvidence("e9", cast(Any, "meeting_visual"), "Google Meet m1", at, at, "document")
    with pytest.raises(ValueError, match="time_granularity"):
        SourceEvidence("e9", "meeting_screen", "Google Meet m1", at, at, cast(Any, "frame"))
