from datetime import UTC, datetime, timedelta, timezone

import pytest

from fakeyou.foundation.timeparse import (
    assume_utc,
    is_aware,
    parse_iso_datetime,
    require_iso_datetime,
)

JAKARTA = timezone(timedelta(hours=7))


def test_offset_timestamp_keeps_its_offset() -> None:
    assert parse_iso_datetime("2026-05-01T10:00:00+07:00") == datetime(
        2026, 5, 1, 10, tzinfo=JAKARTA
    )


def test_z_suffix_is_utc() -> None:
    parsed = parse_iso_datetime("2026-05-01T03:00:00Z")

    assert parsed == datetime(2026, 5, 1, 3, tzinfo=UTC)
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(0)


def test_naive_timestamp_stays_naive() -> None:
    parsed = parse_iso_datetime("2026-05-01T03:00:00")

    assert parsed == datetime(2026, 5, 1, 3)
    assert parsed is not None
    assert parsed.tzinfo is None


@pytest.mark.parametrize(
    "value", ["", "not a time", "2026-13-01T00:00:00Z", "2026-05-01T03:00:00z"]
)
def test_unparseable_text_is_none(value: str) -> None:
    assert parse_iso_datetime(value) is None


def test_require_parses_like_parse() -> None:
    assert require_iso_datetime("2026-05-01T03:00:00Z") == datetime(2026, 5, 1, 3, tzinfo=UTC)


def test_require_raises_value_error_for_unparseable_text() -> None:
    with pytest.raises(ValueError, match="Invalid isoformat string"):
        require_iso_datetime("not a time")


def test_assume_utc_labels_naive_timestamps() -> None:
    labelled = assume_utc(datetime(2026, 5, 1, 3))

    assert labelled == datetime(2026, 5, 1, 3, tzinfo=UTC)
    assert labelled.tzinfo is UTC


def test_assume_utc_keeps_aware_timestamps() -> None:
    aware = datetime(2026, 5, 1, 10, tzinfo=JAKARTA)

    assert assume_utc(aware) is aware


def test_is_aware_requires_a_utc_offset() -> None:
    assert is_aware(datetime(2026, 5, 1, 3, tzinfo=UTC)) is True
    assert is_aware(datetime(2026, 5, 1, 3)) is False
