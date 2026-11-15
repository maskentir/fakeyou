from datetime import UTC, datetime, timedelta
from typing import Any

from fakeyou.foundation.clock_mapping import MappedInstant
from fakeyou.foundation.content_digest import HEARTBEAT_WITHOUT_RECORD, DigestRecord
from fakeyou.perception.samples import SampleLedger
from tests.test_content_digest import pixels

START = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return START + timedelta(seconds=seconds)


def sample(sequence: int, digest: str | None = "aa", **changes: Any) -> DigestRecord:
    return pixels(digest, **{"sequence": sequence, "media_timestamp": 1000.0 * sequence, **changes})


def ingest(ledger: SampleLedger, record: DigestRecord, seconds: float) -> str:
    return ledger.ingest(
        record, captured_at=at(seconds), capture=MappedInstant(100.0 + seconds, 0.01)
    ).outcome


def test_accepted_sample_becomes_the_latest_with_its_own_capture() -> None:
    ledger = SampleLedger()

    assert ingest(ledger, sample(1), 0.0) == "accepted"
    assert ingest(ledger, sample(2, "bb"), 0.5) == "accepted"

    latest = ledger.latest("screen", "screen")
    assert latest is not None
    assert latest.record.digest == "bb"
    assert latest.captured_at == at(0.5)
    assert latest.capture == MappedInstant(100.5, 0.01)
    assert ledger.revision == 2


def test_repeated_media_timestamp_is_not_a_new_capture() -> None:
    ledger = SampleLedger()
    ingest(ledger, sample(1), 0.0)

    outcome = ingest(ledger, sample(2, media_timestamp=1000.0), 30.0)

    latest = ledger.latest("screen", "screen")
    assert outcome == "repeated"
    assert latest is not None
    assert latest.record.sequence == 1
    assert latest.captured_at == at(0.0)
    assert ledger.irregularities("screen", "screen")["repeated"] == 1


def test_lower_or_equal_sequence_is_discarded() -> None:
    ledger = SampleLedger()
    ingest(ledger, sample(5), 0.0)

    assert ingest(ledger, sample(5, "bb"), 0.5) == "out_of_order"
    assert ingest(ledger, sample(3, "bb"), 0.5) == "out_of_order"

    latest = ledger.latest("screen", "screen")
    assert latest is not None
    assert latest.record.digest == "aa"
    assert ledger.irregularities("screen", "screen")["discarded"] == 2
    assert ledger.revision == 1


def test_higher_sequence_with_an_earlier_capture_time_is_marked_inconsistent() -> None:
    ledger = SampleLedger()
    ingest(ledger, sample(1), 5.0)

    assert ingest(ledger, sample(2), 4.0) == "inconsistent_time"
    inconsistent = ledger.latest("screen", "screen")
    assert inconsistent is not None
    assert not inconsistent.consistent

    assert ingest(ledger, sample(3), 6.0) == "accepted"
    recovered = ledger.latest("screen", "screen")
    assert recovered is not None
    assert recovered.consistent


def test_sequence_gap_is_counted_and_the_later_sample_still_becomes_latest() -> None:
    ledger = SampleLedger()
    ingest(ledger, sample(1), 0.0)

    assert ingest(ledger, sample(4, "bb"), 2.0) == "accepted"

    assert ledger.irregularities("screen", "screen")["gaps"] == 1
    latest = ledger.latest("screen", "screen")
    assert latest is not None
    assert latest.record.sequence == 4


def test_skipped_hash_is_stored_without_reusing_the_previous_digest() -> None:
    ledger = SampleLedger()
    ingest(ledger, sample(1), 0.0)

    ingest(ledger, sample(2, None, skip_reason="hash_over_budget"), 0.5)

    latest = ledger.latest("screen", "screen")
    assert latest is not None
    assert latest.record.digest is None
    assert latest.record.skip_reason == "hash_over_budget"


def test_sources_are_independent_and_stopping_one_advances_the_revision() -> None:
    ledger = SampleLedger()
    ingest(ledger, sample(1), 0.0)
    ingest(ledger, sample(1, role="camera", subject_id="device-budi"), 0.0)

    ledger.stop("screen", "screen")
    ledger.stop("screen", "screen")

    assert ledger.stopped("screen", "screen")
    assert not ledger.stopped("camera", "device-budi")
    assert ledger.revision == 3
    assert ledger.next_sequence("camera", "device-budi") == 2
    assert ledger.next_sequence("camera", "device-ayu") == 1


def test_new_sample_after_a_stop_reopens_the_source() -> None:
    ledger = SampleLedger()
    ingest(ledger, sample(1), 0.0)
    ledger.stop("screen", "screen")

    ingest(ledger, sample(2), 1.0)

    assert not ledger.stopped("screen", "screen")


def test_heartbeat_record_carries_no_digest() -> None:
    ledger = SampleLedger()
    heartbeat = sample(1, None, skip_reason=HEARTBEAT_WITHOUT_RECORD, media_timestamp=None)

    ledger.ingest(heartbeat, captured_at=at(0.0), capture=None)

    latest = ledger.latest("screen", "screen")
    assert latest is not None
    assert latest.record.same_content_as(sample(1)) is None
    assert latest.capture is None


def test_restarted_stream_accepts_reset_sequence_and_media_timestamp() -> None:
    ledger = SampleLedger()
    ingest(ledger, sample(500, stream_id="old-stream"), 0.0)
    ledger.stop("screen", "screen")

    outcome = ingest(ledger, sample(1, stream_id="new-stream", media_timestamp=1.0), 1.0)

    assert outcome == "accepted"
    latest = ledger.latest("screen", "screen")
    assert latest is not None
    assert latest.record.stream_id == "new-stream"
    assert latest.record.sequence == 1
    assert latest.record.media_timestamp == 1.0
    assert latest.continuity > 0


def test_late_retired_stream_cannot_replace_the_restarted_stream() -> None:
    ledger = SampleLedger()
    ingest(ledger, sample(500, stream_id="old-stream"), 0.0)
    ledger.stop("screen", "screen")
    ingest(ledger, sample(1, stream_id="new-stream", media_timestamp=1.0), 1.0)

    outcome = ingest(ledger, sample(501, stream_id="old-stream"), 2.0)

    assert outcome == "retired_stream"
    latest = ledger.latest("screen", "screen")
    assert latest is not None
    assert latest.record.stream_id == "new-stream"


def test_same_stream_id_new_sampler_generation_can_reset_counters() -> None:
    ledger = SampleLedger()
    ingest(ledger, sample(500, generation=1), 0.0)
    ledger.stop("screen", "screen")

    assert ingest(ledger, sample(1, generation=2, media_timestamp=1.0), 1.0) == "accepted"
    assert ingest(ledger, sample(501, generation=1), 2.0) == "retired_stream"
    assert ingest(ledger, sample(2, generation=2, media_timestamp=2.0), 2.0) == "accepted"
    latest = ledger.latest("screen", "screen")
    assert latest is not None
    assert latest.record.generation == 2
    assert latest.record.sequence == 2


def test_stopped_sampler_cannot_reopen_itself_with_a_late_packet() -> None:
    ledger = SampleLedger()
    ingest(ledger, sample(500, generation=1), 0.0)
    ledger.stop("screen", "screen")

    assert ingest(ledger, sample(501, generation=1), 1.0) == "retired_stream"
    assert ledger.stopped("screen", "screen")


def test_retired_camera_sampler_cannot_be_relabelled_as_a_new_subject() -> None:
    ledger = SampleLedger()
    ingest(ledger, sample(500, role="camera", subject_id="old-person", generation=1), 0.0)
    ledger.stop("camera", "old-person")

    outcome = ingest(ledger, sample(501, role="camera", subject_id="new-person", generation=1), 1.0)

    assert outcome == "retired_stream"
    assert ledger.latest("camera", "new-person") is None
