import json
from dataclasses import replace
from importlib.resources import files
from typing import Any

import pytest

from fakeyou.conversation.visual_speech import RuntimeSpeech, TurnIdentity
from fakeyou.foundation.abstention import (
    NOT_IN_CATALOG,
    AbstentionRef,
    abstention_digest,
    abstention_entry,
    abstention_refusal,
    catalog_entry,
    catalog_languages,
    catalog_version,
    select_abstention,
)
from fakeyou.foundation.clock_mapping import LOCAL_MONOTONIC
from fakeyou.foundation.publication import (
    SentenceBinding,
    decide_visual_publication,
    sentence_digest,
)
from fakeyou.perception.inspector import VisualInspection
from tests._persistence_fakes import RecordingJournal
from tests.test_publication_activation import Meeting, ask

AB1 = "I don't have enough reliable information to answer that."
AB2 = "I couldn't determine that from the frame I checked."
AB3 = "I couldn't inspect the visual information needed to answer."
ASSET = json.loads(files("fakeyou").joinpath("assets", "publication_catalog.json").read_text())


def indonesian(entry_id: str) -> str:
    return str(ASSET["abstentions"][entry_id]["text"]["id"])


def test_catalog_v1_holds_three_entries_in_english_and_indonesian() -> None:
    assert catalog_version() == 1
    assert catalog_languages() == {"en", "id"}
    assert [abstention_entry(name, "en").text for name in ("AB1", "AB2", "AB3")] == [
        AB1,
        AB2,
        AB3,
    ]
    assert [abstention_entry(name, "en").scope for name in ("AB1", "AB2", "AB3")] == [
        "no_visual",
        "historical",
        "no_visual",
    ]
    for name in ("AB1", "AB2", "AB3"):
        entry = abstention_entry(name, "id")
        assert entry.text == indonesian(name)
        assert entry.text != abstention_entry(name, "en").text
        assert entry.scope == abstention_entry(name, "en").scope


@pytest.mark.parametrize(
    ("status", "inspected", "expected"),
    [
        (None, False, "AB1"),
        ("produced", True, "AB1"),
        ("insufficient", True, "AB2"),
        ("insufficient", False, "AB3"),
        ("unavailable", False, "AB3"),
        ("unavailable", True, "AB3"),
    ],
)
@pytest.mark.parametrize("language", ["en", "id"])
def test_entry_is_selected_by_its_condition(
    status: str | None, *, inspected: bool, expected: str, language: str
) -> None:
    entry = select_abstention(look_status=status, frame_inspected=inspected, language=language)

    assert (entry.entry_id, entry.language) == (expected, language)


def test_unknown_language_falls_back_to_english() -> None:
    assert abstention_entry("AB1", "fr").language == "en"
    assert abstention_entry("AB1", None).language == "en"


def ref_for(text: str, entry_id: str = "AB1", **changes: Any) -> AbstentionRef:
    ref = AbstentionRef(entry_id, 1, "en", abstention_digest(text))
    return replace(ref, **changes)


def binding(text: str, ref: AbstentionRef, scope: str = "no_visual") -> SentenceBinding:
    return SentenceBinding(
        "publication",
        "meeting-9",
        "turn",
        "turn",
        "request",
        0,
        sentence_digest(text),
        None,
        (),
        (),
        scope,
        abstention=ref,
    )


def decided(text: str, ref: AbstentionRef, scope: str = "no_visual") -> tuple[str, str]:
    decision = decide_visual_publication(
        binding(text, ref, scope), {}, at=100.0, clock_domain=LOCAL_MONOTONIC
    )
    return decision.validity, decision.reason


def test_catalog_text_is_a_valid_abstention() -> None:
    assert decided(AB1, ref_for(AB1)) == ("valid", "ok")


@pytest.mark.parametrize(
    "text", ["  i DON'T have   enough reliable information to answer that. ", AB1.upper()]
)
def test_spacing_and_case_do_not_change_the_entry(text: str) -> None:
    assert decided(text, ref_for(text)) == ("valid", "ok")


@pytest.mark.parametrize(
    "text",
    [
        "I don't have enough reliable information to answer that, but revenue is $1.2M.",
        "I don't have enough reliable data to answer that.",
        "I don't have enough reliable information to answer that. Revenue is $1.2M.",
        "Revenue is $1.2M.",
        "There is nothing on the screen.",
    ],
)
def test_text_that_is_not_exactly_an_entry_is_refused(text: str) -> None:
    assert decided(text, ref_for(text)) == ("unknown", NOT_IN_CATALOG)


@pytest.mark.parametrize("change", [{"version": 2}, {"language": "fr"}, {"entry_id": "AB9"}])
def test_reference_to_a_version_language_or_entry_that_does_not_exist_is_refused(
    change: dict[str, Any],
) -> None:
    assert decided(AB1, ref_for(AB1, **change)) == ("unknown", NOT_IN_CATALOG)


def test_entry_used_under_another_scope_is_refused() -> None:
    assert abstention_refusal(ref_for(AB1), "historical") == NOT_IN_CATALOG
    assert abstention_refusal(ref_for(AB2, "AB2"), "no_visual") == NOT_IN_CATALOG
    assert abstention_refusal(ref_for(AB2, "AB2"), "historical") is None


def test_abstention_can_never_be_bound_as_a_current_claim() -> None:
    with pytest.raises(ValueError, match="never a current claim"):
        binding(AB1, ref_for(AB1), "current")


def test_catalog_lookup_returns_the_entry_of_a_reference() -> None:
    entry = catalog_entry(abstention_entry("AB3", "id").ref)

    assert entry is not None
    assert entry.text == indonesian("AB3")


async def test_engine_refuses_to_bind_text_that_differs_from_the_abstention_digest() -> None:
    meeting = Meeting()
    prepared = await meeting.prepare("Revenue is $1.2M.", sentence_index=0, transcript_id="t")

    with pytest.raises(ValueError, match="abstention digest"):
        meeting.engine.bind_publication(
            prepared, replace(binding("Revenue is $1.2M.", ref_for(AB1)), transcript_id="t")
        )
    await meeting.engine.close()


async def test_engine_prevents_an_abstention_that_is_not_in_the_catalog() -> None:
    meeting = Meeting()
    text = "Revenue is $1.2M."
    prepared = await meeting.prepare(text, sentence_index=0, transcript_id="t")
    meeting.engine.bind_publication(
        prepared, replace(binding(text, ref_for(text)), transcript_id="t")
    )

    await meeting.engine.speak_prepared(prepared)

    assert meeting.prevented == f"unknown:{NOT_IN_CATALOG}"
    await meeting.engine.close()


@pytest.mark.parametrize("language", ["en", "id"])
async def test_insufficient_look_is_answered_with_the_inspected_frame_entry(
    language: str,
) -> None:
    meeting = Meeting(inspections=(VisualInspection(verdict="unclear"),))

    turn = await ask(
        meeting,
        "When is beta?",
        needs=("screen",),
        reply="There is nothing on the screen.",
        language=language,
    )

    [bound] = meeting.bindings
    [receipt] = meeting.settled
    assert receipt.text == abstention_entry("AB2", language).text
    assert receipt.outcome == "completed"
    assert receipt.local_audio_written
    assert (bound.scope, bound.gather_attempt_id is not None) == ("historical", True)
    assert [item.status for item in bound.evidence] == ["insufficient"]
    assert len(turn.counted) == 1
    assert turn.replier.calls == []


@pytest.mark.parametrize("language", ["en", "id"])
async def test_look_that_inspected_no_frame_is_answered_with_the_uninspected_entry(
    language: str,
) -> None:
    meeting = Meeting()
    meeting.perception.interpreter = None

    turn = await ask(
        meeting,
        "When is beta?",
        needs=("screen",),
        reply="I'm not sure, but beta is 17 March.",
        language=language,
    )

    [bound] = meeting.bindings
    [receipt] = meeting.settled
    assert receipt.text == abstention_entry("AB3", language).text
    assert (bound.scope, bound.evidence) == ("no_visual", ())
    assert bound.abstention is not None
    assert bound.abstention.entry_id == "AB3"
    assert receipt.outcome == "completed"
    assert len(turn.counted) == 1


async def test_without_a_publication_context_the_reply_is_spoken_as_today() -> None:
    meeting = Meeting(inspections=(VisualInspection(verdict="unclear"),), gated=False)

    await ask(meeting, "When is beta?", needs=("screen",), reply="I cannot see that on the screen.")

    assert meeting.bindings == []
    assert [item.text for item in meeting.settled] == ["I cannot see that on the screen."]


async def test_only_one_abstention_is_spoken_per_turn_and_the_look_is_journaled() -> None:
    meeting = Meeting()
    journal = RecordingJournal()
    context = meeting.publication_context
    assert context is not None
    indices = iter(range(10))
    speech = RuntimeSpeech(
        meeting, context, TurnIdentity("t", "t:1", "en"), lambda: next(indices), journal
    )

    first = await speech.abstain(None, ("screen",), reason="look_not_produced")
    second = await speech.abstain(None, ("screen",), reason="claim_undeclared")

    assert (first, second) == (True, False)
    assert [item.text for item in meeting.settled] == [AB3]
    [selected] = journal.events("abstention_selected")
    assert selected["entry_id"] == "AB3"
    assert selected["catalog_version"] == 1
    [skipped] = journal.events("abstention_skipped")
    assert skipped["reason"] == "claim_undeclared"
    await meeting.engine.close()
