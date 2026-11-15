from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, override

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fakeyou.audio.playback import PlaybackEvent, PlaybackEventKind
from fakeyou.foundation.clock_mapping import LOCAL_MONOTONIC
from fakeyou.foundation.publication import (
    PublicationDecision,
    PublicationValidity,
    SentenceBinding,
    SnapshotRef,
    Validation,
)
from fakeyou.observability.participant_audio import (
    AUDIO_MEASUREMENT_SCOPE,
    VERIFIED_IDENTITY_METHODS,
    consent_reference,
    measure_participant_publication,
    observe_participant,
)
from fakeyou.orchestration.spoken_log import SpokenLog
from fakeyou.perception.revalidation import SceneRevalidator
from fakeyou.persistence.record import MeetingRecord, TwinSpeechEntry
from fakeyou.speech.contract import PlaybackOutcome, PlaybackReceipt
from tests._participant_audio_fakes import (
    ScriptedParticipantObserver,
    granted,
    observer_clock,
)
from tests._persistence_fakes import RecordingJournal
from tests.test_publication_activation import (
    CAPTURED_AT,
    Meeting,
    Turn,
    ask,
    camera_goes_stale,
    show_budget,
)
from tests.test_publication_measurement import _binding, _post

NO_WRITE: frozenset[PlaybackOutcome] = frozenset({"prevented", "dropped_before_audio"})
UNSETTLED: frozenset[PlaybackOutcome | None] = frozenset({None, "attempted"})
GATE_CHECKED_AT = 99.9
AUDIO_NOT_BEFORE = 99.89
HEARD_FROM = 100.0
HEARD_UNTIL = 101.0
ClockKind = Literal["mapped", "uncertain", "none"]
PostKind = Literal["continuous", "missing", "changed"]


@dataclass(frozen=True, slots=True)
class Case:
    validity: PublicationValidity | None
    receipt: PlaybackOutcome | None
    observer: bool
    consent_state: Literal["granted", "denied", "unknown", "revoked"]
    consent_scope: str
    consent_source: str
    granted_at: float | None
    revoked_at: float | None
    identity_method: str
    clock: ClockKind
    heard: bool
    right_sentence: bool
    capture_mapped: bool
    provider_known: bool
    post: PostKind
    caller_local_write: bool | None = None
    queried_at: float | None = HEARD_UNTIL + 0.2
    consent_domain: str = "fake"

    @property
    def grant_complete(self) -> bool:
        return (
            self.consent_state == "granted"
            and self.consent_scope == AUDIO_MEASUREMENT_SCOPE
            and bool(self.consent_source)
            and self.granted_at is not None
            and (self.revoked_at is None or self.revoked_at > self.granted_at)
        )

    @property
    def read_permitted(self) -> bool:
        return (
            self.grant_complete
            and self.validity is not None
            and self.queried_at is not None
            and self.consent_domain == "fake"
            and self.granted_at is not None
            and self.granted_at <= AUDIO_NOT_BEFORE
            and (self.revoked_at is None or self.revoked_at > self.queried_at)
        )

    def decision(self) -> PublicationDecision | None:
        if self.validity is None:
            return None
        return PublicationDecision(self.validity, "scripted", GATE_CHECKED_AT, "fake", 0.01)

    @property
    def no_local_write(self) -> bool:
        if self.receipt in NO_WRITE:
            return True
        return self.receipt in UNSETTLED and self.caller_local_write is False

    @property
    def consent_in_force(self) -> bool:
        return self.read_permitted and (self.revoked_at is None or self.revoked_at > HEARD_UNTIL)

    @property
    def usable_observation(self) -> bool:
        return (
            self.observer
            and self.consent_in_force
            and self.identity_method in VERIFIED_IDENTITY_METHODS
            and self.clock == "mapped"
            and self.heard
            and self.right_sentence
        )

    @property
    def fully_evidenced(self) -> bool:
        return (
            self.validity == "valid"
            and self.usable_observation
            and not self.no_local_write
            and self.capture_mapped
            and self.provider_known
            and self.post == "continuous"
        )

    def binding(self) -> SentenceBinding:
        binding = _binding(95.1 if self.capture_mapped else None)
        if self.provider_known:
            return binding
        [evidence] = binding.evidence
        return replace(binding, evidence=(replace(evidence, provider=None),))

    def scripted(self) -> ScriptedParticipantObserver | None:
        if not self.observer:
            return None
        clock = {
            "mapped": observer_clock(),
            "uncertain": observer_clock(round_trip=0.2),
            "none": None,
        }[self.clock]
        return ScriptedParticipantObserver(
            consented=granted(
                state=self.consent_state,
                scope=self.consent_scope,
                source=self.consent_source,
                granted_at=self.granted_at,
                revoked_at=self.revoked_at,
                clock_domain=self.consent_domain,
            ),
            clock=clock,
            heard_from=HEARD_FROM if self.heard else None,
            heard_until=HEARD_UNTIL,
            identity_method=self.identity_method,
            reported_hash=None if self.right_sentence else "another-sentence",
        )

    def measure(self) -> dict[str, Any]:
        decision = self.decision()
        changed = SnapshotRef("camera", "subject", "stream", 1, 2, 2, datetime.now(UTC))
        post = {
            "continuous": _post(),
            "missing": None,
            "changed": _post(snap=changed),
        }[self.post]
        receipt = (
            None
            if self.receipt is None
            else PlaybackReceipt("attempt", self.receipt, "Sentence.", publication_id="pub")
        )
        return measure_participant_publication(
            self.binding(),
            self.scripted(),
            decision=decision,
            clock_domain="fake",
            receipt=receipt,
            post_validations=post,
            queried_at=self.queried_at,
            **(
                {}
                if self.caller_local_write is None
                else {"local_write_observed": self.caller_local_write}
            ),
        )


CASES = st.builds(
    Case,
    validity=st.sampled_from(["valid", "invalid", "unknown", None]),
    receipt=st.sampled_from(
        [
            None,
            "attempted",
            "prevented",
            "dropped_before_audio",
            "audio_started",
            "interrupted_after_audio",
            "completed",
        ]
    ),
    observer=st.booleans(),
    consent_state=st.sampled_from(["granted", "granted", "denied", "unknown", "revoked"]),
    consent_scope=st.sampled_from([AUDIO_MEASUREMENT_SCOPE, AUDIO_MEASUREMENT_SCOPE, "marketing"]),
    consent_source=st.sampled_from(["participant_confirmed", "participant_confirmed", ""]),
    granted_at=st.sampled_from([10.0, 10.0, AUDIO_NOT_BEFORE, 99.895, 100.5, 150.0, None]),
    revoked_at=st.sampled_from([None, None, 100.5, 150.0]),
    identity_method=st.sampled_from(["watermark", "fingerprint", "transcript", "timing_guess"]),
    clock=st.sampled_from(["mapped", "mapped", "uncertain", "none"]),
    heard=st.booleans(),
    right_sentence=st.booleans(),
    capture_mapped=st.booleans(),
    provider_known=st.booleans(),
    post=st.sampled_from(["continuous", "continuous", "missing", "changed"]),
    caller_local_write=st.sampled_from([None, None, True, False]),
    queried_at=st.sampled_from([HEARD_UNTIL + 0.2, HEARD_UNTIL + 0.2, 200.0, None]),
    consent_domain=st.sampled_from(["fake", "fake", "", "page_wall_utc"]),
)


@settings(max_examples=2000, deadline=None, derandomize=True)
@given(CASES)
def test_no_combination_of_partial_evidence_yields_a_compliant_verdict(case: Case) -> None:
    result = case.measure()
    status = result["status"]

    assert (status == "observed_compliant") is case.fully_evidenced
    assert (result.get("compliant") is True) is case.fully_evidenced
    if status.startswith("observed_"):
        assert case.usable_observation
        assert not case.no_local_write
    if status.startswith("prevented_"):
        assert case.validity in ("invalid", "unknown")
        assert case.no_local_write
        assert not case.usable_observation
    if result["participant_gap"] is not None:
        assert "compliant" not in result
    if not case.observer:
        assert result["participant_gap"] == "participant_observer_unavailable"


@settings(max_examples=500, deadline=None, derandomize=True)
@given(CASES)
def test_observer_is_read_only_when_the_grant_is_proven_in_force(case: Case) -> None:
    observer = case.scripted()
    if observer is None:
        return

    observation = observe_participant(
        case.binding(), observer, consent_reference(case.decision(), case.queried_at)
    )

    assert observer.heard_calls == (1 if case.read_permitted else 0)
    if not case.read_permitted:
        assert observation.publication is None
        assert observation.gap is not None


FULL = Case(
    validity="valid",
    receipt="completed",
    observer=True,
    consent_state="granted",
    consent_scope=AUDIO_MEASUREMENT_SCOPE,
    consent_source="participant_confirmed",
    granted_at=10.0,
    revoked_at=None,
    identity_method="watermark",
    clock="mapped",
    heard=True,
    right_sentence=True,
    capture_mapped=True,
    provider_known=True,
    post="continuous",
)
REMOVALS: list[tuple[dict[str, Any], str, str]] = [
    ({"observer": False}, "not_measurable", "participant_observer_unavailable"),
    ({"consent_state": "unknown"}, "not_measurable", "participant_consent_unknown"),
    ({"consent_state": "denied"}, "not_measurable", "participant_consent_denied"),
    ({"consent_state": "revoked"}, "not_measurable", "participant_consent_revoked"),
    ({"consent_scope": "marketing"}, "not_measurable", "participant_consent_scope_mismatch"),
    ({"consent_source": ""}, "not_measurable", "participant_consent_unknown"),
    ({"granted_at": None}, "not_measurable", "participant_consent_not_in_force"),
    ({"granted_at": 100.5}, "not_measurable", "participant_consent_not_in_force"),
    ({"granted_at": 99.895}, "not_measurable", "participant_consent_not_in_force"),
    ({"granted_at": 150.0}, "not_measurable", "participant_consent_not_in_force"),
    ({"revoked_at": 100.5}, "not_measurable", "participant_consent_revoked"),
    (
        {"revoked_at": 150.0, "queried_at": 200.0},
        "not_measurable",
        "participant_consent_revoked",
    ),
    ({"queried_at": None}, "not_measurable", "participant_consent_reference_missing"),
    ({"consent_domain": ""}, "not_measurable", "participant_consent_clock_incomparable"),
    (
        {"consent_domain": "page_wall_utc"},
        "not_measurable",
        "participant_consent_clock_incomparable",
    ),
    ({"identity_method": "transcript"}, "not_measurable", "publication_identity_unverified"),
    ({"clock": "none"}, "not_measurable", "participant_clock_unmapped"),
    ({"clock": "uncertain"}, "not_measurable", "participant_clock_unmeasurable"),
    ({"heard": False}, "not_measurable", "participant_sentence_not_reported"),
    ({"right_sentence": False}, "not_measurable", "publication_identity_mismatch"),
    ({"capture_mapped": False}, "not_measurable", "capture_age_unmeasurable"),
    ({"provider_known": False}, "not_measurable", "provider_identity_unmeasurable"),
    ({"post": "missing"}, "not_measurable", "postpublication_continuity_unmeasurable"),
    ({"post": "changed"}, "observed_noncompliant", "scene_not_continuous"),
    ({"validity": None}, "not_measurable", "participant_consent_reference_missing"),
    ({"validity": "invalid"}, "observed_noncompliant", "published_despite_gate_invalid"),
    ({"validity": "unknown"}, "observed_noncompliant", "published_despite_gate_unknown"),
    ({"receipt": "prevented"}, "not_measurable", "publication_observation_conflict"),
    ({"receipt": "dropped_before_audio"}, "not_measurable", "publication_observation_conflict"),
    (
        {"receipt": "prevented", "caller_local_write": True},
        "not_measurable",
        "publication_observation_conflict",
    ),
    (
        {"receipt": "dropped_before_audio", "caller_local_write": True},
        "not_measurable",
        "publication_observation_conflict",
    ),
    (
        {"receipt": None, "caller_local_write": False},
        "not_measurable",
        "publication_observation_conflict",
    ),
]


def test_caller_claim_of_no_write_cannot_hide_a_receipt_that_reports_audio() -> None:
    result = replace(FULL, caller_local_write=False).measure()

    assert result["status"] == "observed_compliant"


@pytest.mark.parametrize("kind", ["playback_finished", "playback_dropped"])
def test_playback_event_without_a_receipt_never_enters_the_meeting_record(
    kind: PlaybackEventKind, tmp_path: Path
) -> None:
    journal = RecordingJournal()
    record = MeetingRecord(tmp_path / "record.jsonl")
    log = SpokenLog(journal, record)
    try:
        log.observe(PlaybackEvent("playback_started", "The slide shows revenue."))
        log.observe(PlaybackEvent(kind, "The slide shows revenue.", heard="The slide"))
        assert [entry for entry in record.entries() if isinstance(entry, TwinSpeechEntry)] == []
        assert len(journal.events(kind)) == 1
    finally:
        record.close()


def test_full_evidence_is_the_only_compliant_control() -> None:
    assert FULL.fully_evidenced
    assert FULL.measure()["status"] == "observed_compliant"


@pytest.mark.parametrize(("removal", "status", "reason"), REMOVALS)
def test_removing_one_piece_of_evidence_removes_the_compliant_verdict(
    removal: dict[str, Any], status: str, reason: str
) -> None:
    result = replace(FULL, **removal).measure()

    assert (result["status"], result["reason"]) == (status, reason)
    assert result.get("compliant") is not True


@dataclass
class Audited(Meeting):
    decisions: dict[str, PublicationDecision | None] = field(default_factory=dict)

    @override
    def __post_init__(self) -> None:
        super().__post_init__()
        context = self.publication_context
        assert context is not None
        inner = context.gate
        assert inner is not None

        async def recording(binding: SentenceBinding) -> PublicationDecision | None:
            decision = await inner(binding)
            self.decisions[binding.publication_id] = decision
            return decision

        self.publication_context = replace(context, gate=recording)
        self.engine.configure_publication(self.publication_context)


def participant(*, heard: bool = True, **changes: Any) -> ScriptedParticipantObserver:
    observer = ScriptedParticipantObserver(
        **{
            "heard_from": CAPTURED_AT + 2.1 if heard else None,
            "heard_until": CAPTURED_AT + 2.7,
            "clock_domain": LOCAL_MONOTONIC,
            **changes,
        }
    )
    observer.consented = replace(observer.consented, clock_domain=LOCAL_MONOTONIC)
    return observer


async def continuity(
    meeting: Meeting, binding: SentenceBinding, at: float
) -> dict[str, Validation]:
    meeting.clock.at = at
    revalidate = SceneRevalidator(meeting.perception, now=meeting.clock)
    return {item.kind: await revalidate(item) for item in binding.evidence}


async def measured(
    turn: Turn, observer: ScriptedParticipantObserver | None, *, continuity_at: float | None
) -> dict[str, Any]:
    meeting = turn.meeting
    assert isinstance(meeting, Audited)
    [binding] = meeting.bindings
    [receipt] = meeting.settled
    post = None if continuity_at is None else await continuity(meeting, binding, continuity_at)
    return measure_participant_publication(
        binding,
        observer,
        decision=meeting.decisions.get(binding.publication_id),
        clock_domain=LOCAL_MONOTONIC,
        receipt=receipt,
        post_validations=post,
        queried_at=AFTER_SPEECH,
    )


AFTER_SPEECH = CAPTURED_AT + 2.8


async def screen_turn(**options: Any) -> Turn:
    return await ask(Audited(**options), "When is beta?", needs=("screen",))


async def test_end_to_end_control_reaches_compliant_only_with_every_fake_in_place() -> None:
    turn = await screen_turn()

    result = await measured(turn, participant(), continuity_at=AFTER_SPEECH)

    assert result["status"] == "observed_compliant"
    assert len(turn.counted) == 1


async def test_end_to_end_without_an_observer_is_the_production_outcome() -> None:
    turn = await screen_turn()

    result = await measured(turn, None, continuity_at=AFTER_SPEECH)

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "participant_observer_unavailable",
    )
    assert turn.meeting.writer.written


@pytest.mark.parametrize(
    ("observer", "reason"),
    [
        (participant(consented=granted(state="unknown")), "participant_consent_unknown"),
        (participant(consented=granted(state="denied")), "participant_consent_denied"),
        (participant(consented=granted(at=CAPTURED_AT + 2.4)), "participant_consent_not_in_force"),
        (participant(clock=None), "participant_clock_unmapped"),
        (participant(identity_method="transcript"), "publication_identity_unverified"),
        (participant(heard=False), "participant_sentence_not_reported"),
        (
            participant(clock_domain="observer_wall"),
            "participant_consent_clock_incomparable",
        ),
    ],
)
async def test_end_to_end_observer_gaps_never_become_verdicts(
    observer: ScriptedParticipantObserver, reason: str
) -> None:
    turn = await screen_turn()

    result = await measured(turn, observer, continuity_at=AFTER_SPEECH)

    assert (result["status"], result["reason"]) == ("not_measurable", reason)


async def test_end_to_end_without_post_speech_continuity_is_not_measurable() -> None:
    turn = await screen_turn()

    result = await measured(turn, participant(), continuity_at=None)

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "postpublication_continuity_unmeasurable",
    )


async def test_end_to_end_scene_change_after_speech_is_noncompliant() -> None:
    turn = await screen_turn()
    show_budget(turn.meeting)

    result = await measured(turn, participant(), continuity_at=AFTER_SPEECH)

    assert (result["status"], result["reason"]) == (
        "observed_noncompliant",
        "scene_not_continuous",
    )


async def test_end_to_end_prevented_claim_is_prevented_not_compliant() -> None:
    turn = await screen_turn(before_write=show_budget)

    result = await measured(turn, participant(heard=False), continuity_at=AFTER_SPEECH)

    assert result["status"] == "prevented_invalid"
    assert turn.counted == []
    assert turn.meeting.writer.written == []


async def test_end_to_end_report_of_a_prevented_claim_is_a_conflict_not_a_verdict() -> None:
    turn = await screen_turn(before_write=show_budget)

    result = await measured(turn, participant(), continuity_at=AFTER_SPEECH)

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "publication_observation_conflict",
    )


async def test_end_to_end_unmapped_capture_is_prevented_unknown() -> None:
    turn = await screen_turn(mapped=False)

    result = await measured(turn, participant(heard=False), continuity_at=AFTER_SPEECH)

    assert result["status"] == "prevented_unknown"
    assert result["reason"] == "capture_time_unmapped"


async def test_end_to_end_camera_claim_with_a_lost_camera_is_prevented_unknown() -> None:
    turn = await ask(
        Audited(before_write=camera_goes_stale),
        "What is Budi wearing?",
        needs=("participant",),
        reply="Budi wears grey.",
    )

    result = await measured(turn, participant(heard=False), continuity_at=AFTER_SPEECH)

    assert result["status"] == "prevented_unknown"


PATHS: list[tuple[str, dict[str, Any], dict[str, Any], int]] = [
    ("visual allowed", {}, {"needs": ("screen",)}, 1),
    ("visual prevented", {"before_write": show_budget}, {"needs": ("screen",)}, 0),
    ("visual unmapped", {"mapped": False}, {"needs": ("screen",)}, 0),
    ("no visual reply", {}, {"reply": "I am well."}, 1),
    ("state allowed", {}, {"state_question": "presenter"}, 1),
    ("non-visual state", {}, {"state_question": "participant_count"}, 1),
]


@pytest.mark.parametrize(("name", "options", "turn_options", "spoken"), PATHS)
async def test_every_speech_path_counts_exactly_the_attempts_with_local_audio(
    name: str, options: dict[str, Any], turn_options: dict[str, Any], spoken: int
) -> None:
    del name
    turn = await ask(Audited(**options), "Question?", **turn_options)
    meeting = turn.meeting

    voiced = {item.attempt_id for item in meeting.receipts if item.local_audio_written}
    assert len(turn.counted) == len(voiced) == spoken
    assert bool(meeting.writer.written) is bool(spoken)
    assert len({item.attempt_id for item in meeting.receipts}) == 1
    assert [item.outcome for item in meeting.settled] == (
        ["completed"] if spoken else ["prevented"]
    )


@pytest.mark.parametrize(
    ("options", "turn_options"),
    [
        ({}, {"needs": ("screen",)}),
        ({"before_write": show_budget}, {"needs": ("screen",)}),
        ({"mapped": False}, {"needs": ("screen",)}),
        ({"look_at": CAPTURED_AT + 299.0, "write_at": CAPTURED_AT + 300.0}, {"needs": ("screen",)}),
        ({}, {"state_question": "presenter"}),
        ({}, {"reply": "I am well."}),
    ],
)
async def test_default_path_without_a_context_speaks_everything_and_binds_nothing(
    options: dict[str, Any], turn_options: dict[str, Any]
) -> None:
    turn = await ask(Meeting(gated=False, **options), "Question?", **turn_options)
    meeting = turn.meeting

    assert meeting.bindings == []
    assert meeting.writer.written
    assert [item.outcome for item in meeting.settled] == ["completed"]
    assert all(item.publication_id is None for item in meeting.receipts)
    assert len(turn.counted) == 1
