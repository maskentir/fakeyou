import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from fakeyou.audio.playback import FRAME_BYTES, PlaybackEvent
from fakeyou.foundation.publication import (
    EvidenceBinding,
    PublicationContext,
    PublicationDecision,
    SentenceBinding,
    SnapshotRef,
    Validation,
    evaluate_visual_publication,
)
from tests._audio_support import FakeSynth, FakeWriter, build_engine


def _binding(text: str, publication_id: str = "pub-1", sentence_index: int = 2) -> SentenceBinding:
    return SentenceBinding(
        publication_id,
        "meeting",
        "turn",
        "transcript",
        "request",
        sentence_index,
        hashlib.sha256(" ".join(text.split()).encode()).hexdigest(),
        None,
        (),
        (),
        "no_visual",
    )


def _current_binding(text: str, *, capture: float | None) -> SentenceBinding:
    snapshot = SnapshotRef(
        "screen", "subject", "stream", 1, 1, 1, datetime(2026, 11, 14, tzinfo=UTC)
    )
    evidence = EvidenceBinding(
        "screen",
        "gather",
        snapshot,
        99.0,
        "fake",
        "attempt",
        "description",
        capture_monotonic=capture,
        capture_clock_domain="utc",
        capture_mapping_clock_domain="test",
        capture_time_origin="source_capture",
        capture_mapping_uncertainty_seconds=0.0,
        observation_clock_domain="test",
    )
    return SentenceBinding(
        "policy-pub",
        "meeting",
        "turn",
        "transcript",
        "request",
        2,
        hashlib.sha256(" ".join(text.split()).encode()).hexdigest(),
        "gather",
        ("screen",),
        (evidence,),
        "current",
    )


async def test_valid_bound_publication_writes_audio_with_identity() -> None:
    events: list[PlaybackEvent] = []
    writer = FakeWriter()
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), writer, events)

    async def gate(binding: SentenceBinding) -> PublicationDecision:
        assert binding.publication_id == "pub-1"
        return PublicationDecision("valid", "ok", 1.0, "test")

    engine.configure_publication(PublicationContext("meeting", "test", gate, lambda: 1.0))
    prepared = await engine.prepare(
        "  hello   world ", sentence_index=2, transcript_id="transcript"
    )
    assert prepared is not None
    engine.bind_publication(prepared, _binding(prepared.text))
    try:
        await engine.speak_prepared(prepared)
        assert writer.written
        assert events[1].utterance.publication is not None
        assert events[1].utterance.publication.publication_id == "pub-1"
    finally:
        await engine.close()


async def test_invalid_bound_publication_writes_no_audio() -> None:
    events: list[PlaybackEvent] = []
    writer = FakeWriter()
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), writer, events)

    async def run_gate(binding: SentenceBinding) -> PublicationDecision:
        del binding
        return PublicationDecision("invalid", "stale", 1.0, "test")

    async def _invalid() -> PublicationDecision:
        return PublicationDecision("invalid", "stale", 1.0, "test")

    engine.configure_publication(PublicationContext("meeting", "test", run_gate, lambda: 1.0))
    prepared = await engine.prepare("hello world", sentence_index=2, transcript_id="transcript")
    assert prepared is not None
    engine.bind_publication(prepared, _binding(prepared.text))
    try:
        await engine.speak_prepared(prepared)
        assert writer.written == []
        assert events[-1].kind == "playback_dropped"
        assert "stale" in events[-1].detail
    finally:
        await engine.close()


async def test_freeze_during_gate_prevents_write_without_rerunning_gate() -> None:
    events: list[PlaybackEvent] = []
    writer = FakeWriter()
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), writer, events)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def gate(_binding: SentenceBinding) -> PublicationDecision:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return PublicationDecision("valid", "ok", 1.0, "test")

    engine.configure_publication(PublicationContext("meeting", "test", gate, lambda: 1.0))
    prepared = await engine.prepare("hello world", sentence_index=2, transcript_id="transcript")
    assert prepared is not None
    engine.bind_publication(prepared, _binding(prepared.text))
    playing = asyncio.create_task(engine.speak_prepared(prepared))
    try:
        await entered.wait()
        engine.freeze()
        release.set()
        await playing
        assert calls == 1
        assert writer.written == []
        assert events[-1].detail == "publication_prevented:unknown:frozen"
    finally:
        release.set()
        await engine.close()


async def test_gate_timeout_prevents_current_clip_and_worker_continues() -> None:
    events: list[PlaybackEvent] = []
    writer = FakeWriter()
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), writer, events)
    calls = 0

    async def gate(_binding: SentenceBinding) -> PublicationDecision:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(1)
        return PublicationDecision("valid", "ok", 1.0, "test")

    engine.configure_publication(
        PublicationContext("meeting", "test", gate, lambda: 1.0, timeout_seconds=0.01)
    )
    first = await engine.prepare("first", sentence_index=2, transcript_id="transcript")
    second = await engine.prepare("second", sentence_index=3, transcript_id="transcript")
    assert first is not None
    assert second is not None
    engine.bind_publication(first, _current_binding(first.text, capture=1.0))
    engine.bind_publication(
        second,
        replace(
            _current_binding(second.text, capture=1.0), publication_id="second", sentence_index=3
        ),
    )
    try:
        await engine.speak_prepared(first)
        await engine.speak_prepared(second)
        assert calls == 2
        assert writer.written
        assert any(event.detail.startswith("publication_prevented:unknown") for event in events)
    finally:
        await engine.close()


async def test_cancellation_during_gate_writes_no_pcm() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    writer = FakeWriter()
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), writer, [])

    async def gate(_binding: SentenceBinding) -> PublicationDecision:
        entered.set()
        await release.wait()
        return PublicationDecision("valid", "ok", 1.0, "test")

    engine.configure_publication(PublicationContext("meeting", "test", gate, lambda: 1.0))
    prepared = await engine.prepare("cancel", sentence_index=2, transcript_id="transcript")
    assert prepared is not None
    engine.bind_publication(prepared, _binding(prepared.text))
    playing = asyncio.create_task(engine.speak_prepared(prepared))
    try:
        await entered.wait()
        playing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await playing
        assert writer.written == []
    finally:
        release.set()
        await engine.close()


async def test_no_visual_binding_without_gate_is_allowed() -> None:
    writer = FakeWriter()
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), writer, [])
    engine.configure_publication(PublicationContext("meeting", "test", None, lambda: 1.0))
    prepared = await engine.prepare("safe", sentence_index=2, transcript_id="transcript")
    assert prepared is not None
    engine.bind_publication(prepared, _binding(prepared.text))
    try:
        await engine.speak_prepared(prepared)
        assert writer.written
    finally:
        await engine.close()


async def test_replayed_sentence_requires_distinct_publication_ids() -> None:
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    checked: list[str] = []
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), writer, events)

    async def gate(binding: SentenceBinding) -> PublicationDecision:
        checked.append(binding.publication_id)
        return PublicationDecision("valid", "ok", 1.0, "test")

    engine.configure_publication(PublicationContext("meeting", "test", gate, lambda: 1.0))
    first = await engine.prepare("same", sentence_index=2, transcript_id="transcript")
    second = await engine.prepare("same", sentence_index=2, transcript_id="transcript")
    assert first is not None
    assert second is not None
    engine.bind_publication(first, _binding(first.text, "pub-1"))
    engine.bind_publication(second, _binding(second.text, "pub-2"))
    try:
        await engine.speak_prepared(first)
        await engine.speak_prepared(second)
        assert len(writer.written) >= 2
        assert checked == ["pub-1", "pub-2"]
        assert [
            event.utterance.publication.publication_id
            for event in events
            if event.kind == "playback_audio_started" and event.utterance.publication is not None
        ] == checked
    finally:
        await engine.close()


async def test_duplicate_publication_id_is_rejected() -> None:
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), FakeWriter(), [])
    first = await engine.prepare("same", sentence_index=2, transcript_id="transcript")
    second = await engine.prepare("same", sentence_index=2, transcript_id="transcript")
    assert first is not None
    assert second is not None
    engine.bind_publication(first, _binding(first.text, "pub-1"))
    with pytest.raises(ValueError, match="publication identity"):
        engine.bind_publication(second, _binding(second.text, "pub-1"))
    await engine.close()


async def test_queued_clip_uses_write_boundary_clock() -> None:
    now = [0.0]
    events: list[PlaybackEvent] = []
    writer = FakeWriter()
    writer.release.clear()
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), writer, events)

    async def gate(_binding: SentenceBinding) -> PublicationDecision:
        return PublicationDecision(
            "valid" if now[0] <= 0.25 else "invalid",
            "ok" if now[0] <= 0.25 else "expired",
            now[0],
            "test",
        )

    engine.configure_publication(PublicationContext("meeting", "test", gate, lambda: now[0]))
    first = await engine.prepare("first", sentence_index=2, transcript_id="transcript")
    second = await engine.prepare("second", sentence_index=3, transcript_id="transcript")
    assert first is not None
    assert second is not None
    engine.bind_publication(first, _binding(first.text, "queued-1"))
    engine.bind_publication(second, _binding(second.text, "queued-2", sentence_index=3))
    first_task = asyncio.create_task(engine.speak_prepared(first))
    second_task = asyncio.create_task(engine.speak_prepared(second))
    try:
        await writer.started.wait()
        now[0] = 1.0
        writer.release.set()
        await first_task
        await second_task
        assert any("expired" in event.detail for event in events)
        assert events[-1].utterance.publication is not None
        assert events[-1].utterance.publication.publication_id == "queued-2"
    finally:
        writer.release.set()
        await engine.close()


async def test_barge_in_during_gate_allows_newer_clip_only() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    events: list[PlaybackEvent] = []
    writer = FakeWriter()
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), writer, events)

    async def gate(binding: SentenceBinding) -> PublicationDecision:
        if binding.publication_id == "old":
            entered.set()
            await release.wait()
        return PublicationDecision("valid", "ok", 1.0, "test")

    engine.configure_publication(PublicationContext("meeting", "test", gate, lambda: 1.0))
    old = await engine.prepare("old", sentence_index=2, transcript_id="transcript")
    new = await engine.prepare("new", sentence_index=3, transcript_id="transcript")
    assert old is not None
    assert new is not None
    engine.bind_publication(old, _binding(old.text, "old"))
    engine.bind_publication(new, _binding(new.text, "new", sentence_index=3))
    old_task = asyncio.create_task(engine.speak_prepared(old))
    try:
        await entered.wait()
        await engine.stop()
        release.set()
        await old_task
        await engine.speak_prepared(new)
        starts = [event for event in events if event.kind == "playback_audio_started"]
        assert len(starts) == 1
        assert starts[-1].utterance.publication is not None
        assert starts[-1].utterance.publication.publication_id == "new"
    finally:
        release.set()
        await engine.close()


async def test_prevented_clip_does_not_add_pcm_after_previous_audio() -> None:
    events: list[PlaybackEvent] = []
    writer = FakeWriter()
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), writer, events)
    calls = 0

    async def gate(_binding: SentenceBinding) -> PublicationDecision:
        nonlocal calls
        calls += 1
        return PublicationDecision("valid" if calls == 1 else "invalid", "stale", 1.0, "test")

    engine.configure_publication(PublicationContext("meeting", "test", gate, lambda: 1.0))
    first = await engine.prepare("first", sentence_index=2, transcript_id="transcript")
    second = await engine.prepare("second", sentence_index=3, transcript_id="transcript")
    assert first is not None
    assert second is not None
    engine.bind_publication(first, _binding(first.text, "fade-1"))
    engine.bind_publication(second, _binding(second.text, "fade-2", sentence_index=3))
    try:
        await engine.speak_prepared(first)
        written = list(writer.written)
        await engine.speak_prepared(second)
        assert writer.written == written
        assert any(event.detail.endswith(":stale") for event in events)
    finally:
        await engine.close()


async def test_missing_participant_observer_does_not_block_valid_current_clip() -> None:
    writer = FakeWriter()
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), writer, [])

    async def valid_gate(_binding: SentenceBinding) -> PublicationDecision:
        return PublicationDecision("valid", "observer_unavailable", 1.0, "test")

    engine.configure_publication(PublicationContext("meeting", "test", valid_gate, lambda: 1.0))
    prepared = await engine.prepare("current", sentence_index=2, transcript_id="transcript")
    assert prepared is not None
    binding = _current_binding(prepared.text, capture=1.0)
    engine.bind_publication(prepared, binding)
    try:
        await engine.speak_prepared(prepared)
        assert writer.written
    finally:
        await engine.close()


async def test_visual_policy_rejects_old_capture_at_write_boundary() -> None:
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), writer, events)
    clock = [100.0]

    async def revalidate(evidence: EvidenceBinding) -> Validation:
        return Validation(
            checked_at=100.0,
            clock_domain="test",
            current_snapshot=evidence.snapshot,
            supported=True,
            unchanged=True,
            observed_at=99.0,
            uncertainty_seconds=0.0,
        )

    async def gate(binding: SentenceBinding) -> PublicationDecision:
        return await evaluate_visual_publication(
            binding, revalidate, now=lambda: clock[0], clock_domain="test"
        )

    engine.configure_publication(PublicationContext("meeting", "test", gate, lambda: clock[0]))
    prepared = await engine.prepare("old capture", sentence_index=2, transcript_id="transcript")
    assert prepared is not None
    binding = _current_binding(prepared.text, capture=90.0)
    engine.bind_publication(prepared, binding)
    try:
        await engine.speak_prepared(prepared)
        assert writer.written == []
        assert events[-1].detail == "publication_prevented:invalid:capture_too_old"
        assert binding.evidence[0].capture_monotonic == 90.0
        assert binding.evidence[0].observed_at == 99.0
        assert binding.evidence[0].capture_time_origin == "source_capture"
    finally:
        await engine.close()


async def test_visual_policy_unmapped_capture_stays_unknown() -> None:
    writer = FakeWriter()
    events: list[PlaybackEvent] = []
    engine = build_engine(FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), writer, events)
    clock = [100.0]

    async def revalidate(evidence: EvidenceBinding) -> Validation:
        return Validation(
            checked_at=100.0,
            clock_domain="test",
            current_snapshot=evidence.snapshot,
            supported=True,
            unchanged=True,
            observed_at=99.0,
            uncertainty_seconds=0.0,
        )

    async def gate(binding: SentenceBinding) -> PublicationDecision:
        return await evaluate_visual_publication(
            binding, revalidate, now=lambda: clock[0], clock_domain="test"
        )

    engine.configure_publication(PublicationContext("meeting", "test", gate, lambda: clock[0]))
    prepared = await engine.prepare("unmapped", sentence_index=2, transcript_id="transcript")
    assert prepared is not None
    engine.bind_publication(prepared, _current_binding(prepared.text, capture=None))
    try:
        await engine.speak_prepared(prepared)
        assert writer.written == []
        assert events[-1].detail == "publication_prevented:unknown:capture_time_unmapped"
    finally:
        await engine.close()


@pytest.mark.parametrize("scope", ["no_visual", "historical"])
async def test_visual_policy_allows_safe_scope_without_current_liveness(scope: str) -> None:
    writer = FakeWriter()
    engine = build_engine(FakeSynth(), writer, [])

    async def revalidate(_evidence: EvidenceBinding) -> Validation:
        raise AssertionError("safe scope must not require current liveness")

    async def gate(binding: SentenceBinding) -> PublicationDecision:
        return await evaluate_visual_publication(
            binding, revalidate, now=lambda: 100.0, clock_domain="test"
        )

    engine.configure_publication(PublicationContext("meeting", "test", gate, lambda: 100.0))
    prepared = await engine.prepare("safe", sentence_index=2, transcript_id="transcript")
    assert prepared is not None
    binding = (
        _binding(prepared.text)
        if scope == "no_visual"
        else replace(_current_binding(prepared.text, capture=None), scope="historical")
    )
    engine.bind_publication(prepared, binding)
    try:
        await engine.speak_prepared(prepared)
        assert writer.written
    finally:
        await engine.close()
