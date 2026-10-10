import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.diarize_client import DiarizedTurn, DiarizeError
from fakeyou.ingestion_status import write_atomically
from fakeyou.record import RecordEntry, TranscriptEntry, load_meeting_record

SCHEMA = "diarization_correction/v1"
RTTM_FILE_NAME = "diarization.rttm"
JSON_FILE_NAME = "diarization.json"


class StaleDiarizationError(DiarizeError):
    pass


def correction_digest(record_path: Path) -> str | None:
    try:
        raw = (record_path.parent / JSON_FILE_NAME).read_bytes()
    except OSError:
        return None
    return sha256(raw).hexdigest()


@dataclass(frozen=True)
class CorrectionBundle:
    rttm_path: Path
    json_path: Path


def correction_uuid(speaker: str, start_ms: int) -> str:
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, "diarization-correction/v1:" + speaker + ":" + str(start_ms))
    )


def _rttm_line(file_id: str, turn: DiarizedTurn) -> str:
    start = turn.start_ms / 1000
    duration = (turn.end_ms - turn.start_ms) / 1000
    return f"SPEAKER {file_id} 1 {start:.3f} {duration:.3f} <NA> <NA> {turn.speaker} <NA>"


def _check_speaker_labels(turns: list[DiarizedTurn]) -> None:
    for turn in turns:
        if not turn.speaker or any(char.isspace() for char in turn.speaker):
            raise FakeYouError(ErrorCode.DIARIZE_SPEAKER_LABEL_INVALID)


def write_correction_bundle(
    audio_dir: Path, record_path: Path, turns: list[DiarizedTurn], *, clock: Callable[[], datetime]
) -> CorrectionBundle:
    _check_speaker_labels(turns)
    record_bytes = record_path.read_bytes()
    payload = {
        "schema": SCHEMA,
        "record_sha256": sha256(record_bytes).hexdigest(),
        "corrected_at": clock().isoformat(),
        "turns": [
            {
                "start_ms": turn.start_ms,
                "end_ms": turn.end_ms,
                "speaker": turn.speaker,
                "corrected_uuid": correction_uuid(turn.speaker, turn.start_ms),
            }
            for turn in turns
        ],
    }
    rttm_path = record_path.parent / RTTM_FILE_NAME
    json_path = record_path.parent / JSON_FILE_NAME
    file_id = audio_dir.parent.name
    rttm = "".join(_rttm_line(file_id, turn) + "\n" for turn in turns)
    write_atomically(rttm_path, rttm.encode("utf-8"))
    write_atomically(json_path, json.dumps(payload, indent=2).encode("utf-8"))
    return CorrectionBundle(rttm_path=rttm_path, json_path=json_path)


def _session_start_page_ms(record_path: Path) -> float:
    slots_path = record_path.parent / "audio" / "slots.json"
    try:
        payload = json.loads(slots_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return 0.0
    anchors = payload.get("anchors") if isinstance(payload, dict) else None
    if not isinstance(anchors, list):
        return 0.0
    starts = [
        anchor["page_ms"]
        for anchor in anchors
        if isinstance(anchor, dict)
        and isinstance(anchor.get("page_ms"), (int, float))
        and not isinstance(anchor.get("page_ms"), bool)
    ]
    return float(min(starts)) if starts else 0.0


def _matching_uuid(turns: list[dict[str, object]], point: float, tolerance_ms: int) -> str | None:
    best: str | None = None
    best_distance: float | None = None
    for turn in turns:
        start = turn["start_ms"]
        end = turn["end_ms"]
        corrected = turn["corrected_uuid"]
        if (
            not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not isinstance(corrected, str)
        ):
            raise DiarizeError(ErrorCode.DIARIZE_TURNS_INVALID)
        low = float(start) - tolerance_ms
        high = float(end) + tolerance_ms
        if not low <= point <= high:
            continue
        if float(start) <= point <= float(end):
            distance = 0.0
        else:
            distance = min(abs(point - float(start)), abs(point - float(end)))
        if best is None or best_distance is None or distance < best_distance:
            best = corrected
            best_distance = distance
    return best


def load_corrected_record(record_path: Path, *, tolerance_ms: int = 2000) -> list[RecordEntry]:
    entries = load_meeting_record(record_path)
    sidecar = record_path.parent / JSON_FILE_NAME
    if not sidecar.exists():
        return entries
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except ValueError as error:
        raise DiarizeError(ErrorCode.DIARIZE_SIDECAR_INVALID) from error
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise DiarizeError(ErrorCode.DIARIZE_SCHEMA_UNSUPPORTED)
    expected = sha256(record_path.read_bytes()).hexdigest()
    if payload.get("record_sha256") != expected:
        raise StaleDiarizationError(ErrorCode.DIARIZE_STALE)
    turns = payload.get("turns")
    if not isinstance(turns, list):
        raise DiarizeError(ErrorCode.DIARIZE_TURNS_INVALID)
    offset = _session_start_page_ms(record_path)
    shifted: list[dict[str, object]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            raise DiarizeError(ErrorCode.DIARIZE_TURNS_INVALID)
        start = turn.get("start_ms")
        end = turn.get("end_ms")
        if (
            isinstance(start, bool)
            or not isinstance(start, (int, float))
            or isinstance(end, bool)
            or not isinstance(end, (int, float))
        ):
            raise DiarizeError(ErrorCode.DIARIZE_TURNS_INVALID)
        shifted.append({**turn, "start_ms": float(start) + offset, "end_ms": float(end) + offset})
    corrected: list[RecordEntry] = []
    for entry in entries:
        if not isinstance(entry, TranscriptEntry):
            corrected.append(entry)
            continue
        corrected.append(
            entry.model_copy(
                update={
                    "corrected_speaker_uuid": _matching_uuid(
                        shifted, entry.capture_ms, tolerance_ms
                    )
                }
            )
        )
    return corrected
