import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from fakeyou.diarize_client import DiarizedTurn
from fakeyou.ingestion_status import write_atomically

SCHEMA = "diarization_correction/v1"
RTTM_FILE_NAME = "diarization.rttm"
JSON_FILE_NAME = "diarization.json"


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
            raise ValueError("speaker label must be a single token without whitespace")


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
