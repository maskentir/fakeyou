import json
import urllib.request
import wave
from collections.abc import Callable
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from fakeyou.audio.diarize_mix import mix_session_slots, split_chunks
from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.output import emit_text
from fakeyou.config.settings import Settings
from fakeyou.diarization.diarize_client import DiarizedTurn, merge_short_turns, transcribe_chunk
from fakeyou.diarization.diarize_correction import (
    JSON_FILE_NAME,
    StaleDiarizationError,
    load_corrected_record,
    write_correction_bundle,
)
from fakeyou.persistence.ingestion_status import record_lock
from fakeyou.persistence.record import TranscriptEntry, load_meeting_record, meeting_record_path

_CHUNK_MAX_MINUTES = 5.0


def _check_slot_alignment(audio_dir: Path) -> None:
    path = audio_dir / "slots.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    slots = payload.get("slots") if isinstance(payload, dict) else None
    if not isinstance(slots, dict):
        raise FakeYouError(ErrorCode.DIARIZE_SLOT_METADATA_INVALID, path=path)
    starts: list[int] = []
    for entry in slots.values():
        start = entry.get("seq_start") if isinstance(entry, dict) else None
        if isinstance(start, bool) or not isinstance(start, int):
            raise FakeYouError(ErrorCode.DIARIZE_SLOT_METADATA_INVALID, path=path)
        starts.append(start)
    if len(set(starts)) > 1:
        raise FakeYouError(ErrorCode.DIARIZE_SLOT_MISALIGNED, path=path)
    if any(start != 0 for start in starts):
        raise FakeYouError(ErrorCode.DIARIZE_SLOT_NOT_AT_ZERO, path=path)


def _chunk_offset_ms(chunk: bytes) -> int:
    with wave.open(BytesIO(chunk), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    return round(frames * 1000 / rate)


def _run_locked(
    record_path: Path,
    audio_dir: Path,
    api_key: str,
    opener: Callable[[urllib.request.Request], Any] | None,
) -> None:
    _check_slot_alignment(audio_dir)
    mixed = mix_session_slots(audio_dir)
    with wave.open(BytesIO(mixed), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    chunks = split_chunks(frames, rate, max_minutes=_CHUNK_MAX_MINUTES)
    turns: list[DiarizedTurn] = []
    offset = 0
    for chunk in chunks:
        for turn in transcribe_chunk(chunk, api_key=api_key, opener=opener):
            turns.append(
                DiarizedTurn(
                    start_ms=turn.start_ms + offset,
                    end_ms=turn.end_ms + offset,
                    speaker=turn.speaker,
                )
            )
        offset += _chunk_offset_ms(chunk)
    merged = merge_short_turns(turns)
    write_correction_bundle(audio_dir, record_path, merged, clock=lambda: datetime.now(UTC))


def diarize_run(
    meeting_id: str,
    *,
    settings: Settings,
    opener: Callable[[urllib.request.Request], Any] | None = None,
) -> int:
    secret = settings.stt.api_key
    api_key = secret.get_secret_value() if secret is not None else ""
    if not api_key.strip():
        raise FakeYouError(ErrorCode.CONFIG_SECRET_MISSING, name=ConfigKey.STT_API_KEY)
    record_path = meeting_record_path(settings.paths.record_dir, meeting_id)
    audio_dir = record_path.parent / "audio"
    try:
        with record_lock(record_path, purpose="diarization"):
            _run_locked(record_path, audio_dir, api_key, opener)
    except (OSError, ValueError, wave.Error) as error:
        raise FakeYouError(ErrorCode.DIARIZE_INPUT_UNREADABLE, reason=str(error)) from error
    return 0


def _sidecar_turn_count(record_path: Path) -> int:
    try:
        payload = json.loads((record_path.parent / JSON_FILE_NAME).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return 0
    turns = payload.get("turns") if isinstance(payload, dict) else None
    return len(turns) if isinstance(turns, list) else 0


def diarize_show(meeting_id: str, *, settings: Settings) -> int:
    record_path = meeting_record_path(settings.paths.record_dir, meeting_id)
    if not (record_path.parent / JSON_FILE_NAME).exists():
        raise FakeYouError(ErrorCode.DIARIZE_CORRECTION_MISSING, meeting_id=meeting_id)
    try:
        entries = load_corrected_record(record_path)
    except StaleDiarizationError as error:
        total = len(
            [
                entry
                for entry in load_meeting_record(record_path)
                if isinstance(entry, TranscriptEntry)
            ]
        )
        emit_text(f"turns: {_sidecar_turn_count(record_path)}")
        emit_text(f"covered: 0/{total} transcript entries")
        emit_text(f"status: stale: {error}")
        raise SystemExit(ExitCode.INPUT_REJECTED) from None
    transcripts = [entry for entry in entries if isinstance(entry, TranscriptEntry)]
    covered = sum(1 for entry in transcripts if entry.corrected_speaker_uuid is not None)
    emit_text(f"turns: {_sidecar_turn_count(record_path)}")
    emit_text(f"covered: {covered}/{len(transcripts)} transcript entries")
    emit_text("status: ok")
    return 0
