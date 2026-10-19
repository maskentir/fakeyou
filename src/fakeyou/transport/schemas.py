from collections.abc import Mapping

COMMAND_TYPES: tuple[str, ...] = ("join", "stop", "resume")
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

_RESERVED_FIELDS = frozenset({"v", "type", "at", "meeting_id"})
_FORBIDDEN_EVENT_FIELDS = frozenset({"evidence", "api-key"})


def command_stream(meeting_id: str) -> str:
    return f"fakeyou:meeting:{meeting_id}:commands"


def event_stream(meeting_id: str) -> str:
    return f"fakeyou:meeting:{meeting_id}:events"


def encode_command(
    command_type: str, *, meeting_id: str, at: str, fields: Mapping[str, str] | None = None
) -> dict[str, str]:
    if command_type not in COMMAND_TYPES:
        raise ValueError(f"unknown command type: {command_type!r}")
    encoded = {"v": "1", "type": command_type, "at": at, "meeting_id": meeting_id}
    if fields is not None:
        reserved = _RESERVED_FIELDS.intersection(fields)
        if reserved:
            raise ValueError(f"command fields must not override {sorted(reserved)}")
        encoded.update(fields)
    return encoded


def encode_ingestion_job(*, meeting_id: str, kind: str, at: str) -> dict[str, str]:
    if kind not in INGESTION_JOB_TYPES:
        raise ValueError(f"unknown ingestion job kind: {kind!r}")
    return {"v": "1", "type": "ingestion_job", "at": at, "meeting_id": meeting_id, "kind": kind}


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
