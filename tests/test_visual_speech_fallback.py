import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

import fakeyou.speech.registry as speech_registry
from fakeyou.conversation.visual_speech import (
    HISTORICAL_REASONS,
    RuntimeSpeech,
    TurnIdentity,
    VisualTurn,
    prevented_reason,
)
from fakeyou.foundation.clock_mapping import LOCAL_MONOTONIC
from fakeyou.foundation.gathered_evidence import GatheredEvidence
from fakeyou.foundation.publication import (
    HISTORICAL_AGE_SECONDS,
    EvidenceBinding,
    RuntimeSynthesis,
    SentenceBinding,
    Validation,
    decide_visual_publication,
    sentence_digest,
)
from fakeyou.foundation.statement import Rendering, statement_digest
from fakeyou.orchestration.publication_gate import opt_in_publication_context
from fakeyou.perception.inspector import VisualInspection
from fakeyou.speech.contract import PlaybackReceipt, PreparedSpeech
from tests._audio_support import FakeSynth
from tests._perception_fakes import START, keyframe
from tests._persistence_fakes import RecordingJournal
from tests.test_commit_boundary import StalledFirstWrite
from tests.test_publication_activation import (
    BETA_CLAIM,
    BETA_HISTORICAL,
    BETA_STATEMENT,
    CAPTURED_AT,
    GREY_HISTORICAL,
    GREY_SHIRT,
    NOT_ENOUGH,
    ROADMAP,
    Meeting,
    Turn,
    ask,
    camera_meeting,
    camera_turn,
    show_budget,
    spoken_texts,
    stop_sharing,
)
from tests.test_publication_freshness import AT, confirm_same, evidence, validated

ANSWER = "Beta launch is 17 March 2027."


def historical_binding(item: EvidenceBinding, text: str = BETA_HISTORICAL) -> SentenceBinding:
    rendering = Rendering(
        "historical.entity_attribute_value",
        1,
        "historical",
        "en",
        "entity_attribute_value",
        (),
        statement_digest(text),
    )
    return SentenceBinding(
        "publication",
        "meeting",
        "turn",
        "turn",
        "request",
        0,
        sentence_digest(text),
        "gather",
        (item.kind,),
        (item,),
        "historical",
        rendering=rendering,
    )


def historical(item: EvidenceBinding, validation: Validation | None = None) -> tuple[str, str]:
    validations = {} if validation is None else {item.kind: validation}
    decision = decide_visual_publication(
        historical_binding(item), validations, at=AT, clock_domain=LOCAL_MONOTONIC
    )
    return decision.validity, decision.reason


@pytest.mark.parametrize(
    ("age", "uncertainty", "expected"),
    [
        (2.0, 0.01, ("valid", "ok")),
        (12.0, 0.01, ("valid", "ok")),
        (29.98, 0.01, ("valid", "ok")),
        (29.995, 0.01, ("invalid", "historical_too_old")),
        (30.0, 0.0, ("valid", "ok")),
        (30.01, 0.0, ("invalid", "historical_too_old")),
        (31.0, 0.01, ("invalid", "historical_too_old")),
        (90.0, 0.01, ("invalid", "historical_too_old")),
    ],
)
def test_historical_statement_is_limited_to_thirty_seconds_from_the_analyzed_capture(
    age: float, uncertainty: float, expected: tuple[str, str]
) -> None:
    assert HISTORICAL_AGE_SECONDS == 30.0
    assert historical(evidence(AT - age, uncertainty=uncertainty)) == expected


def test_historical_statement_needs_a_look_that_produced() -> None:
    insufficient = replace(evidence(98.0), status="insufficient")

    assert historical(insufficient) == ("unknown", "validation_support_unknown")


def test_historical_statement_needs_a_mapped_capture() -> None:
    assert historical(evidence(None)) == ("unknown", "capture_time_unmapped")


def test_historical_statement_is_refused_after_a_subject_change() -> None:
    assert historical(evidence(98.0), validated(subject_changed=True)) == (
        "invalid",
        "subject_changed",
    )


def test_historical_statement_ignores_a_changed_scene_and_a_fresh_confirmation() -> None:
    changed = validated("distinct", None, unchanged=False, source_stopped=True)

    assert historical(evidence(98.0), changed) == ("valid", "ok")
    assert historical(evidence(60.0), validated("exact")) == ("invalid", "historical_too_old")


def test_eligible_reasons_never_include_subject_or_support_problems() -> None:
    excluded = {
        "subject_changed",
        "claim_evidence_mismatch",
        "claim_evidence_missing",
        "claim_relation_unsupported",
        "claim_untraceable",
        "claim_polarity_unsupported",
        "claim_relation_ambiguous",
        "claim_incomplete",
        "validation_support_unknown",
        "capture_time_unmapped",
        "claim_render_unsupported",
        "statement_not_rendered",
        "synthesis_unavailable",
        "unresolved",
    }

    assert not excluded & HISTORICAL_REASONS


def test_prevention_reason_is_read_from_the_receipt_and_only_from_a_prevented_one() -> None:
    prevented = PlaybackReceipt(
        "a", "prevented", "t", detail="publication_prevented:invalid:scene_changed"
    )

    assert prevented_reason(prevented) == "scene_changed"
    assert prevented_reason(replace(prevented, outcome="dropped_before_audio")) is None
    assert prevented_reason(replace(prevented, outcome="completed")) is None
    assert prevented_reason(None) is None


async def turn_at(age: float, **options: Any) -> Meeting:
    written = CAPTURED_AT + age
    meeting = Meeting(look_at=written - 1.0, write_at=written, **options)
    await ask(meeting, "When is beta?", needs=("screen",))
    return meeting


@pytest.mark.parametrize(
    ("age", "spoken"),
    [(8.0, BETA_HISTORICAL), (12.0, BETA_HISTORICAL), (29.9, BETA_HISTORICAL), (31.0, NOT_ENOUGH)],
)
async def test_statement_prevented_by_age_falls_back_by_the_age_of_the_checked_frame(
    age: float, spoken: str
) -> None:
    meeting = await turn_at(age)

    assert meeting.prevented == "invalid:capture_too_old"
    assert spoken_texts(meeting) == [spoken]
    assert BETA_STATEMENT in meeting.synth.texts
    assert BETA_STATEMENT not in spoken_texts(meeting)


async def test_historical_statement_links_the_prevented_publication_and_shares_its_evidence() -> (
    None
):
    meeting = await turn_at(12.0)

    prevented, fallback = meeting.bindings
    assert (prevented.scope, fallback.scope) == ("current", "historical")
    assert fallback.replaces == prevented.publication_id
    assert fallback.publication_id != prevented.publication_id
    assert fallback.evidence == prevented.evidence
    assert fallback.rendering is not None
    assert fallback.rendering.pattern_id == "historical.entity_attribute_value"
    receipts = {item.publication_id: item for item in meeting.settled}
    assert receipts[prevented.publication_id].outcome == "prevented"
    assert receipts[fallback.publication_id].outcome == "completed"


async def test_thirty_seconds_are_measured_from_the_checked_frame_not_the_confirmation() -> None:
    def restart_with_the_same_slide(rig: Meeting) -> None:
        stop_sharing(rig)
        presenter = rig.timeline.participant("device-ayu", "Ayu")
        rig.timeline.show_screen(keyframe(ROADMAP, seconds=39.6), presenter, data=ROADMAP)

    meeting = await turn_at(40.0, before_write=restart_with_the_same_slide)

    assert meeting.prevented == "unknown:source_identity_changed"
    assert [item.detail for item in meeting.settled if item.outcome == "prevented"][1] == (
        "publication_prevented:invalid:historical_too_old"
    )
    assert spoken_texts(meeting) == [NOT_ENOUGH]


async def test_scene_changed_to_another_slide_states_only_what_the_checked_frame_listed() -> None:
    meeting = await turn_at(8.0, before_write=show_budget)

    assert spoken_texts(meeting) == [BETA_HISTORICAL]
    assert "currently" not in " ".join(spoken_texts(meeting))


async def test_unmapped_capture_is_answered_with_an_abstention_not_a_historical_statement() -> None:
    meeting = Meeting(mapped=False)

    await ask(meeting, "When is beta?", needs=("screen",))

    assert meeting.prevented == "unknown:capture_time_unmapped"
    assert spoken_texts(meeting) == [NOT_ENOUGH]
    assert BETA_HISTORICAL not in meeting.synth.texts


async def test_two_prevented_statements_that_cannot_fall_back_get_one_abstention() -> None:
    meeting = Meeting(mapped=False)
    second = {**BETA_CLAIM, "attribute": "Beta launch", "entity": "Beta"}

    await ask(meeting, "When is beta?", needs=("screen",), claims=(BETA_CLAIM, second))

    outcomes = [item.outcome for item in meeting.settled]
    assert outcomes == ["prevented", "prevented", "completed"]
    assert spoken_texts(meeting) == [NOT_ENOUGH]


async def test_one_claim_falls_back_and_one_unsupported_claim_gets_the_abstention() -> None:
    wrong = {**BETA_CLAIM, "value": "18 March 2027"}

    meeting = await turn_at(12.0)
    assert spoken_texts(meeting) == [BETA_HISTORICAL]

    mixed = Meeting(look_at=CAPTURED_AT + 11.0, write_at=CAPTURED_AT + 12.0)
    await ask(mixed, "When is beta?", needs=("screen",), claims=(BETA_CLAIM, wrong))
    assert spoken_texts(mixed) == [BETA_HISTORICAL, NOT_ENOUGH]


async def test_claim_that_fails_a_field_rule_gets_the_abstention() -> None:
    answer = "Beta right now launch is 17 March 2027"
    meeting = Meeting(
        inspections=(replace_answer(answer),), look_at=CAPTURED_AT + 1.0, write_at=CAPTURED_AT + 2.0
    )

    await ask(
        meeting,
        "When is beta?",
        needs=("screen",),
        claims=({**BETA_CLAIM, "entity": "Beta right now"},),
    )

    assert spoken_texts(meeting) == [NOT_ENOUGH]
    assert meeting.synth.texts == [NOT_ENOUGH]


def replace_answer(answer: str) -> VisualInspection:
    return VisualInspection(verdict="supported", answer=answer)


async def test_camera_claim_older_than_thirty_seconds_gets_the_abstention() -> None:
    meeting = camera_meeting(look_at=CAPTURED_AT + 1.0, write_at=CAPTURED_AT + 31.0)

    await camera_turn(meeting)

    assert spoken_texts(meeting) == [NOT_ENOUGH]
    assert [item.outcome for item in meeting.settled] == ["prevented", "completed"]


async def test_camera_stream_that_changed_subject_gets_the_abstention() -> None:
    def another_participant_takes_the_stream(rig: Meeting) -> None:
        frame = rig.timeline.camera_frames.get("device-budi")
        assert frame is not None
        assert frame.content is not None
        rig.timeline.confirm(
            replace(frame.content.record, subject_id="device-ayu", sequence=1),
            captured_at=START + timedelta(seconds=1.5),
            capture=None,
        )

    meeting = camera_meeting(before_write=another_participant_takes_the_stream)

    await camera_turn(meeting)

    assert meeting.prevented == "invalid:subject_changed"
    assert spoken_texts(meeting) == [NOT_ENOUGH]
    assert GREY_HISTORICAL not in spoken_texts(meeting)


async def test_interrupted_statement_is_followed_by_silence_not_an_abstention() -> None:
    writer = StalledFirstWrite()
    meeting = Meeting(writer=writer)
    asking = asyncio.create_task(ask(meeting, "When is beta?", needs=("screen",)))
    await writer.stalled.wait()

    await meeting.engine.stop()
    await asking

    assert meeting.synth.texts == [BETA_STATEMENT]
    assert spoken_texts(meeting) == []


async def test_model_written_historical_binding_is_refused_under_a_render_only_context() -> None:
    meeting = Meeting()
    text = "In the frame I checked, revenue is $1.2M right now."
    prepared = await meeting.prepare(text, sentence_index=0, transcript_id="turn")
    binding = replace(
        historical_binding(evidence(CAPTURED_AT), text), meeting_id="meeting-9", rendering=None
    )
    meeting.engine.bind_publication(prepared, binding)

    await meeting.engine.speak_prepared(prepared)

    assert meeting.prevented == "unknown:statement_not_rendered"
    await meeting.engine.close()


def journaled(turn: Turn, name: str) -> list[dict[str, Any]]:
    return [fields for event, fields in turn.journal.entries if event == name]


def speech_for(meeting: Meeting, journal: RecordingJournal) -> RuntimeSpeech:
    context = meeting.publication_context
    assert context is not None
    indices = iter(range(10))
    return RuntimeSpeech(
        meeting, context, TurnIdentity("turn", "turn:1", "en"), lambda: next(indices), journal
    )


def historical_gather(meeting: Meeting, item: EvidenceBinding) -> GatheredEvidence:
    del meeting
    return GatheredEvidence(
        {"screen": {"code": "ok", "answer": ANSWER}}, "gather", (item,), scope="historical"
    )


async def bound_look(meeting: Meeting) -> EvidenceBinding:
    gathered = await meeting.evidence("When is beta?", ("screen",), "turn")
    assert isinstance(gathered, GatheredEvidence)
    [item] = gathered.provenance
    return replace(item, gather_attempt_id="gather")


async def test_historical_gather_is_rendered_historically_without_a_freshness_check() -> None:
    meeting = Meeting(look_at=CAPTURED_AT + 1.0)
    journal = RecordingJournal()
    item = await bound_look(meeting)
    meeting.advance(CAPTURED_AT + 20.0)
    turn = VisualTurn(speech_for(meeting, journal), historical_gather(meeting, item), ("screen",))

    voiced = await turn.speak(f"It was March. CLAIMS: [{claim_json()}]")

    assert voiced == 1
    assert spoken_texts(meeting) == [BETA_HISTORICAL]
    [binding] = meeting.bindings
    assert (binding.scope, binding.replaces) == ("historical", None)
    assert BETA_STATEMENT not in meeting.synth.texts
    await meeting.engine.close()


def claim_json() -> str:
    return json.dumps(BETA_CLAIM)


async def test_historical_gather_with_an_unsupported_claim_abstains() -> None:
    meeting = Meeting(look_at=CAPTURED_AT + 1.0)
    item = await bound_look(meeting)
    turn = VisualTurn(
        speech_for(meeting, RecordingJournal()), historical_gather(meeting, item), ("screen",)
    )

    await turn.speak('CLAIMS: [{"kind": "screen", "shape": "item_presence", "item": "a chart"}]')

    assert spoken_texts(meeting) == [NOT_ENOUGH]
    assert all(binding.scope != "historical" for binding in meeting.bindings)
    await meeting.engine.close()


async def test_historical_gather_without_a_mapped_capture_abstains() -> None:
    meeting = Meeting(look_at=CAPTURED_AT + 1.0, mapped=False)
    item = await bound_look(meeting)
    turn = VisualTurn(
        speech_for(meeting, RecordingJournal()), historical_gather(meeting, item), ("screen",)
    )

    await turn.speak(f"CLAIMS: [{claim_json()}]")

    assert spoken_texts(meeting) == [NOT_ENOUGH]
    await meeting.engine.close()


async def test_without_authorized_synthesis_a_visual_turn_is_silent_and_journaled() -> None:
    meeting = Meeting(runtime_synthesis="unavailable")

    turn = await ask(meeting, "When is beta?", needs=("screen",))

    skipped = [
        fields for event, fields in turn.journal.entries if event == "runtime_statement_skipped"
    ]
    assert meeting.synth.texts == []
    assert meeting.writer.written == []
    assert meeting.receipts == []
    assert turn.counted == []
    assert [item["reason"] for item in skipped] == ["synthesis_unavailable"] * 2
    assert [item["statement"] for item in skipped] == [BETA_STATEMENT, NOT_ENOUGH]
    assert [item["purpose"] for item in skipped] == ["current", "abstention"]


async def test_unavailable_synthesis_is_the_default_of_an_opt_in_context() -> None:
    context = opt_in_publication_context("meeting", perception=None, kernel=None)

    assert context.runtime_synthesis == "unavailable"
    assert context.render_only


async def test_turn_without_visual_needs_is_not_affected_by_the_synthesis_gate() -> None:
    meeting = Meeting(runtime_synthesis="unavailable")

    await ask(meeting, "How are you?", reply="I am well.")

    assert meeting.synth.texts == ["I am well."]


async def test_nothing_is_synthesized_ahead_of_a_selected_statement() -> None:
    meeting = Meeting()

    assert meeting.synth.texts == []
    await ask(meeting, "How are you?", reply="I am well.")
    assert meeting.synth.texts == ["I am well."]


AUTHORIZED: tuple[RuntimeSynthesis, ...] = ("local",)


@pytest.mark.parametrize("mode", AUTHORIZED)
async def test_every_runtime_synthesis_request_is_journaled_with_its_authorization(
    mode: RuntimeSynthesis,
) -> None:
    meeting = Meeting(runtime_synthesis=mode)

    turn = await ask(meeting, "When is beta?", needs=("screen",))

    requested = [
        fields for event, fields in turn.journal.entries if event == "runtime_preparation_requested"
    ]
    finished = [
        fields for event, fields in turn.journal.entries if event == "runtime_preparation_finished"
    ]
    assert [item["authorization"] for item in requested] == [mode]
    assert [item["purpose"] for item in requested] == ["current"]
    assert [item["text_sha256"] for item in requested] == [sentence_digest(BETA_STATEMENT)]
    assert [item["outcome"] for item in finished] == ["prepared"]


async def test_synthesis_that_is_prevented_from_publication_is_still_in_the_ledger() -> None:
    meeting = Meeting(before_write=show_budget)

    turn = await ask(meeting, "When is beta?", needs=("screen",))

    requested = [
        fields for event, fields in turn.journal.entries if event == "runtime_preparation_requested"
    ]
    assert [item["purpose"] for item in requested] == ["current", "historical"]
    assert meeting.prevented == "invalid:scene_changed"


class BrokenSpeaker:
    def __init__(self, meeting: Meeting, *, hang: bool) -> None:
        self.publication_context = meeting.publication_context
        self.hang = hang
        self.started = asyncio.Event()

    def bind_publication(self, prepared: PreparedSpeech, binding: SentenceBinding) -> None:
        del prepared, binding

    async def prepare(self, text: str, **_: object) -> PreparedSpeech | None:
        del text
        self.started.set()
        if self.hang:
            await asyncio.Event().wait()
        raise OSError("synthesis failed")

    async def speak_prepared(self, prepared: PreparedSpeech) -> None:
        del prepared


async def test_failed_and_cancelled_synthesis_requests_are_in_the_ledger() -> None:
    journal = RecordingJournal()
    meeting = Meeting()
    failing = replace(speech_for(meeting, journal), speaker=BrokenSpeaker(meeting, hang=False))
    hanging_speaker = BrokenSpeaker(meeting, hang=True)
    hanging = replace(speech_for(meeting, journal), speaker=hanging_speaker)

    with pytest.raises(OSError, match="synthesis failed"):
        await failing.prepare(BETA_STATEMENT, "en", purpose="current")
    pending = asyncio.create_task(hanging.prepare(BETA_STATEMENT, "en", purpose="historical"))
    await hanging_speaker.started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert [row["purpose"] for row in journal.events("runtime_preparation_requested")] == [
        "current",
        "historical",
    ]
    assert [row["error"] for row in journal.events("runtime_preparation_failed")] == ["OSError"]
    assert len(journal.events("runtime_preparation_cancelled")) == 1
    assert journal.events("runtime_preparation_finished") == []
    await meeting.engine.close()


async def test_real_speech_provider_is_never_constructed_in_a_fallback_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a speech provider client was constructed")

    monkeypatch.setattr(speech_registry, "build_speech_synthesizer", refuse)
    meeting = await turn_at(12.0)

    assert isinstance(meeting.synth, FakeSynth)
    assert spoken_texts(meeting) == [BETA_HISTORICAL]


async def test_fresh_confirmation_is_still_spoken_as_a_current_statement() -> None:
    written = CAPTURED_AT + 20.0
    meeting = Meeting(
        look_at=written - 1.0,
        write_at=written,
        before_write=lambda rig: confirm_same(rig, at=written - 0.4),
    )

    await ask(meeting, "When is beta?", needs=("screen",))

    assert spoken_texts(meeting) == [BETA_STATEMENT]
    assert GREY_SHIRT["kind"] == "participant"


async def test_provider_mode_without_a_physical_attempt_ledger_fails_closed() -> None:
    meeting = Meeting(runtime_synthesis="provider")

    turn = await ask(meeting, "When is beta?", needs=("screen",))

    assert meeting.synth.texts == []
    assert turn.counted == []
    skipped = [row for event, row in turn.journal.entries if event == "runtime_statement_skipped"]
    assert skipped
    assert all(row["reason"] == "provider_attempt_ledger_unavailable" for row in skipped)
