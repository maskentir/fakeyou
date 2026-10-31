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
DEBRIEF_STREAM = "fakeyou:jobs:debrief"
REEMBED_STREAM = "fakeyou:jobs:reembed"

_FORBIDDEN_EVENT_FIELDS = frozenset({"evidence", "api-key"})


def event_stream(meeting_id: str) -> str:
    return f"fakeyou:meeting:{meeting_id}:events"


def encode_ingestion_job(*, meeting_id: str, kind: str, at: str) -> dict[str, str]:
    if kind not in INGESTION_JOB_TYPES:
        raise ValueError(f"unknown ingestion job kind: {kind!r}")
    return {"v": "1", "type": "ingestion_job", "at": at, "meeting_id": meeting_id, "kind": kind}


def encode_debrief_job(*, meeting_id: str, at: str, force: bool = False) -> dict[str, str]:
    return {
        "v": "1",
        "type": "debrief_job",
        "at": at,
        "meeting_id": meeting_id,
        "force": "true" if force else "false",
    }


def encode_reembed_job(
    *, at: str, owner_id: str, twin_id: str, type: str = "reembed_job"
) -> dict[str, str]:
    if type != "reembed_job":
        raise ValueError(f"unknown reembed job type: {type!r}")
    return {
        "v": "1",
        "type": "reembed_job",
        "at": at,
        "owner_id": owner_id,
        "twin_id": twin_id,
    }


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
