import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.diarization.correction_labels import CorrectionTurn
from fakeyou.diarization.diarize_client import DiarizedTurn, DiarizeError
from fakeyou.foundation.numbers import real_number
from fakeyou.persistence.audio_persist import read_slot_anchors
from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.ingestion_status import finalized_prefix, hash_record_prefix
from fakeyou.persistence.layout import MeetingDir
from fakeyou.persistence.record import (
    RecordEntry,
    TranscriptEntry,
    load_meeting_record,
    parse_meeting_record,
)

SCHEMA = "diarization_correction/v1"


class StaleDiarizationError(DiarizeError):
    pass


def correction_digest(record_path: Path) -> str | None:
    layout = MeetingDir.from_record_path(record_path)
    try:
        sidecar = layout.diarization.read_bytes()
    except OSError:
        return None
    try:
        labels = layout.correction_labels.read_bytes()
    except FileNotFoundError:
        labels = b""
    except OSError:
        return None
    return sha256(sidecar + b"\0" + labels).hexdigest()


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
    prefix_sha256, prefix_bytes = finalized_prefix(record_path)
    payload = {
        "schema": SCHEMA,
        "record_sha256": prefix_sha256,
        "record_bytes": prefix_bytes,
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
    layout = MeetingDir.from_record_path(record_path)
    rttm_path = layout.diarization_rttm
    json_path = layout.diarization
    file_id = audio_dir.parent.name
    rttm = "".join(_rttm_line(file_id, turn) + "\n" for turn in turns)
    write_atomically(rttm_path, rttm.encode("utf-8"))
    write_atomically(json_path, json.dumps(payload, indent=2).encode("utf-8"))
    return CorrectionBundle(rttm_path=rttm_path, json_path=json_path)


def _session_start_page_ms(record_path: Path) -> float:
    audio_dir = MeetingDir.from_record_path(record_path).audio_dir
    try:
        anchors = read_slot_anchors(audio_dir)
    except (TypeError, ValueError) as error:
        raise DiarizeError(
            ErrorCode.DIARIZE_INPUT_UNREADABLE, reason=type(error).__name__
        ) from error
    starts = [
        anchor["page_ms"]
        for anchor in anchors
        if isinstance(anchor.get("page_ms"), (int, float))
        and not isinstance(anchor.get("page_ms"), bool)
    ]
    return float(min(starts)) if starts else 0.0


def _matching_uuid(turns: Sequence[CorrectionTurn], point: float, tolerance_ms: int) -> str | None:
    best: str | None = None
    best_distance: float | None = None
    for turn in turns:
        low = turn.start_ms - tolerance_ms
        high = turn.end_ms + tolerance_ms
        if not low <= point <= high:
            continue
        if turn.start_ms <= point <= turn.end_ms:
            distance = 0.0
        else:
            distance = min(abs(point - turn.start_ms), abs(point - turn.end_ms))
        if best is None or best_distance is None or distance < best_distance:
            best = turn.corrected_uuid
            best_distance = distance
    return best


def _parse_turn(turn: object, offset: float) -> CorrectionTurn:
    if not isinstance(turn, dict):
        raise DiarizeError(ErrorCode.DIARIZE_TURNS_INVALID)
    corrected = turn.get("corrected_uuid")
    speaker = turn.get("speaker")
    start = real_number(turn.get("start_ms"))
    end = real_number(turn.get("end_ms"))
    if (
        not isinstance(corrected, str)
        or not isinstance(speaker, str)
        or start is None
        or end is None
    ):
        raise DiarizeError(ErrorCode.DIARIZE_TURNS_INVALID)
    return CorrectionTurn(
        corrected_uuid=corrected,
        speaker=speaker,
        start_ms=start + offset,
        end_ms=end + offset,
    )


@dataclass(frozen=True, slots=True)
class _Sidecar:
    turns: list[CorrectionTurn]
    record_bytes: int


def _hashed_prefix_bytes(payload: dict[str, object]) -> int:
    record_bytes = payload.get("record_bytes")
    if not isinstance(record_bytes, int) or isinstance(record_bytes, bool) or record_bytes < 0:
        raise DiarizeError(ErrorCode.DIARIZE_SIDECAR_INVALID)
    return record_bytes


def _prefix_matches(record_path: Path, payload: dict[str, object], record_bytes: int) -> bool:
    try:
        digest = hash_record_prefix(record_path, record_bytes)
    except ValueError:
        return False
    return payload.get("record_sha256") == digest


def _read_sidecar(record_path: Path) -> _Sidecar | None:
    sidecar = MeetingDir.from_record_path(record_path).diarization
    if not sidecar.exists():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except ValueError as error:
        raise DiarizeError(ErrorCode.DIARIZE_SIDECAR_INVALID) from error
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise DiarizeError(ErrorCode.DIARIZE_SCHEMA_UNSUPPORTED)
    record_bytes = _hashed_prefix_bytes(payload)
    if not _prefix_matches(record_path, payload, record_bytes):
        raise StaleDiarizationError(ErrorCode.DIARIZE_STALE)
    turns = payload.get("turns")
    if not isinstance(turns, list):
        raise DiarizeError(ErrorCode.DIARIZE_TURNS_INVALID)
    offset = _session_start_page_ms(record_path)
    return _Sidecar([_parse_turn(turn, offset) for turn in turns], record_bytes)


def _prefix_transcript_ids(record_path: Path, record_bytes: int) -> frozenset[str]:
    with record_path.open("rb") as handle:
        prefix = handle.read(record_bytes).decode("utf-8")
    return frozenset(
        entry.id
        for entry in parse_meeting_record(prefix, repair=False)
        if isinstance(entry, TranscriptEntry)
    )


def load_correction_turns(record_path: Path) -> list[CorrectionTurn]:
    sidecar = _read_sidecar(record_path)
    return sidecar.turns if sidecar is not None else []


def load_corrected_record(record_path: Path, *, tolerance_ms: int = 2000) -> list[RecordEntry]:
    entries = load_meeting_record(record_path)
    sidecar = _read_sidecar(record_path)
    if sidecar is None:
        return entries
    covered = _prefix_transcript_ids(record_path, sidecar.record_bytes)
    corrected: list[RecordEntry] = []
    for entry in entries:
        if not isinstance(entry, TranscriptEntry) or entry.id not in covered:
            corrected.append(entry)
            continue
        corrected.append(
            entry.model_copy(
                update={
                    "corrected_speaker_uuid": _matching_uuid(
                        sidecar.turns, entry.capture_ms, tolerance_ms
                    )
                }
            )
        )
    return corrected
