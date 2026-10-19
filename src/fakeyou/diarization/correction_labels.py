import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.layout import MeetingDir
from fakeyou.persistence.record import RecordEntry, TranscriptEntry


@dataclass(frozen=True, slots=True)
class CorrectionTurn:
    corrected_uuid: str
    speaker: str
    start_ms: float
    end_ms: float


@dataclass(frozen=True, slots=True)
class LabelEvidence:
    name: str
    start_ms: float
    end_ms: float


def _overlaps(turn: CorrectionTurn, evidence: LabelEvidence) -> bool:
    return evidence.start_ms <= turn.end_ms and turn.start_ms <= evidence.end_ms


def build_correction_labels(
    turns: Sequence[CorrectionTurn],
    evidence: Sequence[LabelEvidence],
    *,
    previous: Mapping[str, str | None] | None = None,
) -> dict[str, str | None]:
    speakers = {turn.corrected_uuid: turn.speaker for turn in turns}
    bound: dict[str, str | None] = {
        uuid: name for uuid, name in (previous or {}).items() if uuid in speakers
    }
    ordered = sorted(evidence, key=lambda item: (item.start_ms, item.end_ms, item.name))
    fresh: dict[str, str] = {}
    for turn in turns:
        if turn.corrected_uuid in bound or turn.corrected_uuid in fresh:
            continue
        for item in ordered:
            name = item.name.strip()
            if name and _overlaps(turn, item):
                fresh[turn.corrected_uuid] = name
                break
    claimants: dict[str, set[str]] = {}
    for uuid, held in [*bound.items(), *fresh.items()]:
        if held is not None:
            claimants.setdefault(held, set()).add(speakers[uuid])
    for uuid, claimed in fresh.items():
        bound[uuid] = claimed if len(claimants[claimed]) == 1 else None
    return bound


def write_correction_labels(record_path: Path, labels: Mapping[str, str | None]) -> None:
    payload = {uuid: labels[uuid] for uuid in sorted(labels)}
    write_atomically(
        MeetingDir.from_record_path(record_path).correction_labels,
        (json.dumps(payload, indent=2) + "\n").encode("utf-8"),
    )


def _load_correction_labels(record_path: Path) -> dict[str, str | None] | None:
    try:
        raw = json.loads(
            MeetingDir.from_record_path(record_path).correction_labels.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    labels: dict[str, str | None] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            return None
        if value is not None and not isinstance(value, str):
            return None
        labels[key] = value
    return labels


def bind_correction_labels(
    record_path: Path, turns: Sequence[CorrectionTurn], entries: Sequence[RecordEntry]
) -> dict[str, str | None]:
    evidence = [
        LabelEvidence(name=entry.display_claim, start_ms=entry.capture_ms, end_ms=entry.capture_ms)
        for entry in entries
        if isinstance(entry, TranscriptEntry) and entry.display_claim and entry.capture_ms > 0
    ]
    labels = build_correction_labels(turns, evidence, previous=_load_correction_labels(record_path))
    write_correction_labels(record_path, labels)
    return labels


def read_correction_labels(record_path: Path) -> dict[str, str | None]:
    loaded = _load_correction_labels(record_path)
    return loaded if loaded is not None else {}


def correction_labels_valid(record_path: Path) -> bool:
    return _load_correction_labels(record_path) is not None


def correction_speaker_name(letter: str, label: str | None) -> str:
    if label is None:
        return f"Speaker {letter}, unverified"
    return f"Speaker {letter}, shown as {label}, unverified"


def describe_correction_speakers(record_path: Path) -> dict[str, str]:
    try:
        payload = json.loads(
            MeetingDir.from_record_path(record_path).diarization.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return {}
    turns: Any = payload.get("turns") if isinstance(payload, dict) else None
    if not isinstance(turns, list):
        return {}
    labels = read_correction_labels(record_path)
    named: dict[str, str] = {}
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        uuid = turn.get("corrected_uuid")
        speaker = turn.get("speaker")
        if isinstance(uuid, str) and uuid and isinstance(speaker, str) and speaker:
            if uuid not in named:
                named[uuid] = correction_speaker_name(speaker, labels.get(uuid))
    return named
