from collections.abc import Mapping

EVENT_TYPES: tuple[str, ...] = (
    "phase_changed",
    "transcript.final",
    "twin.speech_started",
    "twin.speech_finished",
    "twin.silent",
    "fault",
    "session_ended",
)
INGESTION_JOB_TYPES: tuple[str, ...] = ("ingest", "correct")
INGESTION_STREAM = "fakeyou:jobs:ingestion"

_FORBIDDEN_EVENT_FIELDS = frozenset({"evidence", "api-key"})


def event_stream(meeting_id: str) -> str:
    return f"fakeyou:meeting:{meeting_id}:events"


def encode_ingestion_job(*, meeting_id: str, kind: str, at: str) -> dict[str, str]:
    if kind not in INGESTION_JOB_TYPES:
        raise ValueError(f"unknown ingestion job kind: {kind!r}")
    return {"v": "1", "type": "ingestion_job", "at": at, "meeting_id": meeting_id, "kind": kind}


def _forbidden_event_field(name: str) -> bool:
    normalized = name.strip().lower().replace("_", "-")
    return normalized in _FORBIDDEN_EVENT_FIELDS or normalized.startswith("x-fakeyou-key")


def validate_event(fields: Mapping[str, str], *, meeting_id: str) -> dict[str, str]:
    if fields.get("v") != "1":
        raise ValueError(f"unsupported event version: {fields.get('v')!r}")
    event_type = fields.get("type")
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {event_type!r}")
    if fields.get("meeting_id") != meeting_id:
        raise ValueError("event meeting_id does not match the stream meeting")
    for name in fields:
        if _forbidden_event_field(name):
            raise ValueError(f"event field {name!r} is not allowed on the transport stream")
    return dict(fields)
