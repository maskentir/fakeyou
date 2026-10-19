from datetime import UTC, datetime


def require_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def parse_iso_datetime(value: str) -> datetime | None:
    try:
        moment = require_iso_datetime(value)
    except ValueError:
        return None
    return moment


def assume_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def is_aware(moment: datetime) -> bool:
    return moment.tzinfo is not None and moment.utcoffset() is not None
