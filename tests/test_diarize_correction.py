import json
import uuid
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from fakeyou.diarize_client import DiarizedTurn
from fakeyou.diarize_correction import (
    CorrectionBundle,
    correction_uuid,
    write_correction_bundle,
)

FIXED_NOW = datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC)
RECORD_TEXT = "pineapple-transcript-marker"


def _session(tmp_path: Path) -> tuple[Path, Path]:
    audio_dir = tmp_path / "meetings" / "m1" / "audio"
    audio_dir.mkdir(parents=True)
    record_path = tmp_path / "meetings" / "m1" / "record.jsonl"
    record_path.write_text(RECORD_TEXT + "\n", encoding="utf-8")
    return audio_dir, record_path


def _turns() -> list[DiarizedTurn]:
    return [
        DiarizedTurn(start_ms=0, end_ms=3400, speaker="A"),
        DiarizedTurn(start_ms=3600, end_ms=7200, speaker="B"),
    ]


def _expected_uuid(speaker: str, start_ms: int) -> str:
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, "diarization-correction/v1:" + speaker + ":" + str(start_ms))
    )


def test_write_correction_bundle_writes_both_files_with_exact_schema(tmp_path: Path) -> None:
    audio_dir, record_path = _session(tmp_path)

    bundle = write_correction_bundle(audio_dir, record_path, _turns(), clock=lambda: FIXED_NOW)

    assert isinstance(bundle, CorrectionBundle)
    assert bundle.rttm_path == record_path.parent / "diarization.rttm"
    assert bundle.json_path == record_path.parent / "diarization.json"
    assert bundle.rttm_path.is_file()
    assert bundle.json_path.is_file()
    payload = json.loads(bundle.json_path.read_text(encoding="utf-8"))
    assert payload["schema"] == "diarization_correction/v1"
    assert payload["record_sha256"] == sha256((RECORD_TEXT + "\n").encode("utf-8")).hexdigest()
    assert payload["corrected_at"] == FIXED_NOW.isoformat()
    assert payload["turns"] == [
        {
            "start_ms": 0,
            "end_ms": 3400,
            "speaker": "A",
            "corrected_uuid": _expected_uuid("A", 0),
        },
        {
            "start_ms": 3600,
            "end_ms": 7200,
            "speaker": "B",
            "corrected_uuid": _expected_uuid("B", 3600),
        },
    ]
    assert bundle.rttm_path.read_text(encoding="utf-8").splitlines() == [
        "SPEAKER m1 1 0.000 3.400 <NA> <NA> A <NA>",
        "SPEAKER m1 1 3.600 3.600 <NA> <NA> B <NA>",
    ]


def test_write_correction_bundle_leaves_record_bytes_identical(tmp_path: Path) -> None:
    audio_dir, record_path = _session(tmp_path)
    before = record_path.read_bytes()

    write_correction_bundle(audio_dir, record_path, _turns(), clock=lambda: FIXED_NOW)

    assert record_path.read_bytes() == before


def test_corrected_uuids_are_deterministic_across_runs(tmp_path: Path) -> None:
    audio_dir, record_path = _session(tmp_path)

    first = write_correction_bundle(audio_dir, record_path, _turns(), clock=lambda: FIXED_NOW)
    first_json = first.json_path.read_bytes()
    first_rttm = first.rttm_path.read_bytes()
    second = write_correction_bundle(audio_dir, record_path, _turns(), clock=lambda: FIXED_NOW)

    assert second.json_path.read_bytes() == first_json
    assert second.rttm_path.read_bytes() == first_rttm
    assert correction_uuid("A", 0) == _expected_uuid("A", 0)


def test_sidecar_carries_no_transcript_text(tmp_path: Path) -> None:
    audio_dir, record_path = _session(tmp_path)

    bundle = write_correction_bundle(audio_dir, record_path, _turns(), clock=lambda: FIXED_NOW)

    assert RECORD_TEXT not in bundle.json_path.read_text(encoding="utf-8")
    assert RECORD_TEXT not in bundle.rttm_path.read_text(encoding="utf-8")
