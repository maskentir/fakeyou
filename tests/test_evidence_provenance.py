import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fakeyou.foundation.clock_mapping import (
    LOCAL_MONOTONIC,
    PAGE_CLOCK,
    PAGE_CLOCK_EXCHANGE,
    ClockExchange,
    RemoteClock,
)
from fakeyou.foundation.publication import (
    AudioPublication,
    EvidenceBinding,
    PublicationDecision,
    SentenceBinding,
    Validation,
    evaluate_visual_publication,
)
from fakeyou.observability.publication_measurement import measure_publication
from fakeyou.perception.budget import PerceptionBudget
from fakeyou.perception.context import MeetingEvidence, PerceptionContext
from fakeyou.perception.interpreter import PerceptionInterpreter
from fakeyou.perception.sensors import PERCEPTION_DRAIN_JS, DrainBatch, PerceptionDrain
from fakeyou.perception.timeline import MeetingTimeline
from tests._perception_fakes import START, keyframe, reading, slide_jpeg, vision_model
from tests._persistence_fakes import RecordingJournal
from tests.test_camera_evidence import camera_stack, jpeg_of, put_frame
from tests.test_sensors import sensors

PAGE_AT_START = START.timestamp() * 1000


class Stepping:
    def __init__(self, at: float = 0.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


def exchanged(clock: RemoteClock, *, page_ms: float, sent: float, received: float) -> bool:
    return clock.observe(ClockExchange(page_ms, sent, received))


def test_remote_instant_maps_to_the_exchange_midpoint_with_half_round_trip() -> None:
    now = Stepping(100.02)
    clock = RemoteClock(now=now, drift_ppm=0)
    assert exchanged(clock, page_ms=50_000, sent=100.0, received=100.02)

    mapped = clock.map(48_000)

    assert mapped is not None
    assert mapped.monotonic == pytest.approx(98.01)
    assert mapped.uncertainty_seconds == pytest.approx(0.01)
    assert (mapped.clock_domain, mapped.origin) == (LOCAL_MONOTONIC, PAGE_CLOCK_EXCHANGE)


def test_drift_allowance_grows_with_the_distance_from_the_exchange() -> None:
    clock = RemoteClock(now=Stepping(100.0), drift_ppm=200)
    assert exchanged(clock, page_ms=200_000, sent=100.0, received=100.0)

    near = clock.map(199_000)
    far = clock.map(100_000)

    assert near is not None
    assert near.uncertainty_seconds == pytest.approx(0.0002)
    assert far is not None
    assert far.uncertainty_seconds == pytest.approx(0.02)


def test_the_tightest_recent_exchange_wins() -> None:
    now = Stepping(102.0)
    clock = RemoteClock(now=now, drift_ppm=0)
    assert exchanged(clock, page_ms=50_000, sent=100.0, received=100.4)
    assert exchanged(clock, page_ms=51_000, sent=101.19, received=101.21)
    assert exchanged(clock, page_ms=51_800, sent=101.9, received=102.0)

    mapped = clock.map(50_500)

    assert mapped is not None
    assert mapped.uncertainty_seconds == pytest.approx(0.01)
    assert mapped.monotonic == pytest.approx(100.7)


@pytest.mark.parametrize(
    "exchange",
    [
        ClockExchange(0, 1.0, 1.1),
        ClockExchange(-5, 1.0, 1.1),
        ClockExchange(1000, 1.2, 1.1),
        ClockExchange(math.nan, 1.0, 1.1),
        ClockExchange(1000, 1.0, math.inf),
    ],
)
def test_an_invalid_exchange_is_rejected_and_maps_nothing(exchange: ClockExchange) -> None:
    clock = RemoteClock(now=Stepping(2.0))

    assert clock.observe(exchange) is False
    assert clock.map(900) is None


def test_no_exchange_a_stale_exchange_or_a_distant_instant_maps_nothing() -> None:
    now = Stepping(100.0)
    clock = RemoteClock(now=now, max_exchange_age_seconds=30, max_span_seconds=120)
    assert clock.map(50_000) is None
    assert exchanged(clock, page_ms=500_000, sent=99.9, received=100.0)
    assert clock.map(500_000 - 121_000) is None
    assert clock.map(math.nan) is None
    assert clock.map(499_000) is not None

    now.at = 130.5

    assert clock.map(499_000) is None


class TimedPage:
    def __init__(self, clock: Stepping, round_trip: float, batches: list[dict[str, Any]]) -> None:
        self._clock = clock
        self._round_trip = round_trip
        self._batches = batches

    async def evaluate(self, expression: str, arg: object = None, /) -> object:
        del arg
        assert expression == PERCEPTION_DRAIN_JS
        self._clock.at += self._round_trip
        return self._batches.pop(0)


async def test_the_drain_round_trip_measures_the_page_clock_mapping() -> None:
    sensed, _, _ = sensors()
    clock = Stepping(200.0)
    sensed.monotonic = clock
    page = TimedPage(clock, 0.04, [{"drained_page_ms": PAGE_AT_START}])

    await PerceptionDrain(page, sensed).drain_once()
    mapped = sensed.capture_instant(START - timedelta(seconds=2))

    assert mapped is not None
    assert mapped.monotonic == pytest.approx(198.02)
    assert mapped.uncertainty_seconds == pytest.approx(0.02 + 200e-6 * 2)


async def test_a_page_without_the_drain_timestamp_leaves_the_mapping_absent() -> None:
    sensed, _, _ = sensors()
    clock = Stepping(200.0)
    sensed.monotonic = clock
    page = TimedPage(clock, 0.04, [DrainBatch().model_dump(exclude={"drained_page_ms"})])

    await PerceptionDrain(page, sensed).drain_once()

    assert sensed.capture_instant(START) is None


def screen_context(
    *, provider: str | None, capture_clock: RemoteClock | None, monotonic: Stepping
) -> PerceptionContext:
    timeline = MeetingTimeline()
    presenter = timeline.participant("device-ayu", "Ayu")
    data = slide_jpeg("Launch Timeline", ["Beta: 17 March 2027"])
    timeline.show_screen(keyframe(data, seconds=0), presenter, data=data)
    timeline.read_screen("screen#1", reading("Launch Timeline", text="Beta 17 March 2027"))
    interpreter = PerceptionInterpreter(
        vision_model(answers=["Beta is 17 March 2027."] * 4),
        PerceptionBudget(budget_usd=1.0, max_calls=10),
        max_concurrent=1,
        snapshots=timeline.camera_frames,
        monotonic=monotonic,
        provider=provider,
    )
    context = PerceptionContext(timeline, interpreter, max_chars=6000, clock=lambda: START)
    if capture_clock is not None:
        context.capture_clock = lambda captured_at: capture_clock.map(
            captured_at.timestamp() * 1000
        )
    return context


def mapping(now: Stepping, *, capture_monotonic: float, round_trip: float = 0.02) -> RemoteClock:
    clock = RemoteClock(now=now, drift_ppm=0)
    midpoint = capture_monotonic + 0.5
    assert clock.observe(
        ClockExchange(PAGE_AT_START + 500, midpoint - round_trip / 2, midpoint + round_trip / 2)
    )
    return clock


async def gathered_binding(
    *,
    provider: str | None = "google/gemini-test",
    capture_monotonic: float | None = 98.0,
    round_trip: float = 0.02,
    inferred_at: float = 99.0,
) -> tuple[EvidenceBinding, dict[str, Any], str]:
    monotonic = Stepping(inferred_at)
    clock = (
        None
        if capture_monotonic is None
        else mapping(monotonic, capture_monotonic=capture_monotonic, round_trip=round_trip)
    )
    journal = RecordingJournal()
    context = screen_context(provider=provider, capture_clock=clock, monotonic=monotonic)
    gathered = await MeetingEvidence(context, journal=journal)("When is beta?", ("screen",), "t1")
    [binding] = gathered.provenance
    [resolved] = journal.events("perception_evidence_resolved")
    return binding, resolved, gathered.gather_attempt_id


async def test_production_evidence_carries_capture_mapping_and_provider_attempt() -> None:
    binding, resolved, _ = await gathered_binding()

    assert binding.capture_monotonic == pytest.approx(98.0)
    assert binding.capture_mapping_uncertainty_seconds == pytest.approx(0.01)
    assert binding.capture_clock_domain == PAGE_CLOCK
    assert binding.capture_mapping_clock_domain == LOCAL_MONOTONIC
    assert binding.capture_time_origin == PAGE_CLOCK_EXCHANGE
    assert binding.provider == "google/gemini-test"
    assert binding.provider_attempt_id
    assert binding.provider_attempt_id == resolved["provider_attempt_id"]
    assert resolved["provider"] == "google/gemini-test"
    assert resolved["capture_mapped"] is True
    assert resolved["capture_mapping_uncertainty_ms"] == pytest.approx(10.0)


async def test_observation_time_is_the_inference_time_not_the_capture_time() -> None:
    binding, _, _ = await gathered_binding(capture_monotonic=90.0, inferred_at=99.0)

    assert binding.capture_monotonic == pytest.approx(90.0)
    assert binding.observed_at == pytest.approx(99.0)
    assert binding.observation_clock_domain == LOCAL_MONOTONIC


async def test_unmapped_capture_and_unlabeled_provider_stay_absent() -> None:
    binding, resolved, _ = await gathered_binding(provider=None, capture_monotonic=None)

    assert binding.capture_monotonic is None
    assert binding.capture_mapping_clock_domain is None
    assert binding.capture_mapping_uncertainty_seconds is None
    assert binding.capture_time_origin == "snapshot_ref_reported"
    assert binding.provider is None
    assert "provider" not in resolved
    assert resolved["capture_mapped"] is False
    assert "capture_mapping_uncertainty_ms" not in resolved


async def test_each_inference_has_its_own_provider_attempt() -> None:
    monotonic = Stepping(99.0)
    context = screen_context(provider="p/m", capture_clock=None, monotonic=monotonic)
    evidence = MeetingEvidence(context, journal=RecordingJournal())

    first = (await evidence("When is beta?", ("screen",), "t1")).provenance[0]
    second = (await evidence("When is beta?", ("screen",), "t2")).provenance[0]

    assert first.provider_attempt_id
    assert second.provider_attempt_id
    assert first.provider_attempt_id != second.provider_attempt_id


async def test_a_reused_camera_answer_keeps_the_attempt_that_produced_it() -> None:
    timeline, context, _ = camera_stack(vision_model(answers=["A dark blue shirt."]))
    put_frame(timeline, jpeg_of((120, 120, 120)))

    first = await context.look_at_participant("Budi", "What color is his shirt?")
    second = await context.look_at_participant("Budi", "What color is his shirt?")

    assert (first.acquisition, second.acquisition) == ("inference", "reused")
    assert first.attempt is not None
    assert second.attempt == first.attempt


async def test_a_look_that_never_reached_the_provider_has_no_attempt() -> None:
    timeline, context, _ = camera_stack(vision_model(answers=["unused"]))
    del timeline

    result = await context.look_at_participant("Budi", "What color is his shirt?")

    assert result.status == "unavailable"
    assert result.attempt is None


def sentence(binding: EvidenceBinding, gather_attempt_id: str) -> SentenceBinding:
    return SentenceBinding(
        "pub",
        "meeting",
        "turn",
        "turn",
        "request",
        0,
        "hash",
        gather_attempt_id,
        ("screen",),
        (binding,),
        "current",
    )


async def gate_decision(
    binding: EvidenceBinding, gather_attempt_id: str, *, now: float
) -> tuple[str, str]:
    async def unchanged(item: EvidenceBinding) -> Validation:
        return Validation(
            now,
            LOCAL_MONOTONIC,
            item.snapshot,
            supported=True,
            unchanged=True,
            observed_at=now,
            uncertainty_seconds=0.0,
        )

    decision = await evaluate_visual_publication(
        sentence(binding, gather_attempt_id),
        unchanged,
        now=lambda: now,
        clock_domain=LOCAL_MONOTONIC,
    )
    return decision.validity, decision.reason


async def test_real_provenance_passes_the_capture_freshness_gate() -> None:
    binding, _, attempt = await gathered_binding(capture_monotonic=98.0)

    assert await gate_decision(binding, attempt, now=100.0) == ("valid", "ok")


async def test_old_capture_with_a_recent_observation_is_still_too_old() -> None:
    binding, _, attempt = await gathered_binding(capture_monotonic=90.0, inferred_at=99.0)

    assert await gate_decision(binding, attempt, now=100.0) == ("invalid", "capture_too_old")


async def test_a_slow_exchange_makes_the_capture_time_too_uncertain_to_use() -> None:
    binding, _, attempt = await gathered_binding(capture_monotonic=98.0, round_trip=0.3)

    assert binding.capture_mapping_uncertainty_seconds == pytest.approx(0.15)
    assert await gate_decision(binding, attempt, now=100.0) == (
        "unknown",
        "capture_time_invalid",
    )


async def test_unmapped_capture_never_falls_back_to_the_observation_time() -> None:
    binding, _, attempt = await gathered_binding(capture_monotonic=None, inferred_at=99.9)

    assert await gate_decision(binding, attempt, now=100.0) == (
        "unknown",
        "capture_time_unmapped",
    )


async def test_missing_provider_identity_stays_unmeasurable() -> None:
    binding, _, attempt = await gathered_binding(provider=None)

    result = measure_publication(
        sentence(binding, attempt),
        AudioPublication(
            "pub",
            "turn",
            0,
            "hash",
            100.0,
            101.0,
            LOCAL_MONOTONIC,
            "participant_audible",
            0.01,
            participant_consent=True,
        ),
        decision=PublicationDecision("valid", "ok", 99.9, LOCAL_MONOTONIC),
        clock_domain=LOCAL_MONOTONIC,
    )

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "provider_identity_unmeasurable",
    )


def test_page_clock_fixture_is_a_real_utc_instant() -> None:
    assert datetime.fromtimestamp(PAGE_AT_START / 1000, UTC) == START
