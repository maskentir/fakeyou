import json
import wave
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import httpx2

from fakeyou.audio.diarize_mix import mix_session_slots, split_chunks
from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings, STTSettings, require_secret
from fakeyou.diarization.correction_labels import bind_correction_labels
from fakeyou.diarization.diarize_client import DiarizedTurn, merge_short_turns, transcribe_chunk
from fakeyou.diarization.diarize_correction import (
    StaleDiarizationError,
    load_corrected_record,
    load_correction_turns,
    write_correction_bundle,
)
from fakeyou.observability.provider_faults import trim_detail
from fakeyou.persistence.ingestion_status import record_lock
from fakeyou.persistence.layout import SLOTS_FILE_NAME, MeetingDir
from fakeyou.persistence.record import TranscriptEntry, load_meeting_record, meeting_record_path

_CHUNK_MAX_MINUTES = 5.0


def _check_slot_alignment(audio_dir: Path) -> None:
    path = audio_dir / SLOTS_FILE_NAME
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
    settings: STTSettings,
    transport: httpx2.BaseTransport | None,
) -> None:
    _check_slot_alignment(audio_dir)
    entries = load_meeting_record(record_path)
    mixed = mix_session_slots(audio_dir)
    with wave.open(BytesIO(mixed), "rb") as handle:
        rate = handle.getframerate()
        frames = handle.readframes(handle.getnframes())
    chunks = split_chunks(frames, rate, max_minutes=_CHUNK_MAX_MINUTES)
    turns: list[DiarizedTurn] = []
    offset = 0
    for chunk in chunks:
        for turn in transcribe_chunk(chunk, settings, transport=transport):
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
    bind_correction_labels(record_path, load_correction_turns(record_path), entries)


def diarize_run(
    meeting_id: SessionId,
    *,
    settings: Settings,
    transport: httpx2.BaseTransport | None = None,
) -> ExitCode:
    require_secret(settings.stt.api_key, ConfigKey.STT_API_KEY)
    record_path = meeting_record_path(settings.paths.record_dir, meeting_id)
    audio_dir = MeetingDir(settings.paths.record_dir, meeting_id).audio_dir
    try:
        with record_lock(record_path, purpose="diarization"):
            _run_locked(record_path, audio_dir, settings.stt, transport)
    except (OSError, ValueError, wave.Error) as error:
        reason = trim_detail(f"{type(error).__name__}: {error}") or type(error).__name__
        raise FakeYouError(ErrorCode.DIARIZE_INPUT_UNREADABLE, reason=reason) from error
    return ExitCode.SUCCESS


def sidecar_turn_count(record_path: Path) -> int | None:
    try:
        payload = json.loads(
            MeetingDir.from_record_path(record_path).diarization.read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        return 0
    except ValueError:
        return None
    turns = payload.get("turns") if isinstance(payload, dict) else None
    return len(turns) if isinstance(turns, list) else None


@dataclass(frozen=True, slots=True)
class DiarizeStatus:
    turn_count: int | None
    covered: int
    total: int
    stale_reason: str | None


def _transcripts(entries: Iterable[object]) -> list[TranscriptEntry]:
    return [entry for entry in entries if isinstance(entry, TranscriptEntry)]


def diarize_status(meeting_id: SessionId, *, settings: Settings) -> DiarizeStatus:
    record_path = meeting_record_path(settings.paths.record_dir, meeting_id)
    if not MeetingDir.from_record_path(record_path).diarization.exists():
        raise FakeYouError(ErrorCode.DIARIZE_CORRECTION_MISSING, meeting_id=meeting_id)
    try:
        entries = load_corrected_record(record_path)
    except StaleDiarizationError as error:
        total = len(_transcripts(load_meeting_record(record_path)))
        return DiarizeStatus(
            turn_count=sidecar_turn_count(record_path),
            covered=0,
            total=total,
            stale_reason=str(error),
        )
    transcripts = _transcripts(entries)
    covered = sum(1 for entry in transcripts if entry.corrected_speaker_uuid is not None)
    return DiarizeStatus(
        turn_count=sidecar_turn_count(record_path),
        covered=covered,
        total=len(transcripts),
        stale_reason=None,
    )
