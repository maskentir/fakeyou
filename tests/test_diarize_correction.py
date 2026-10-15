import json
import uuid
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.diarization.diarize_client import DiarizedTurn, DiarizeError
from fakeyou.diarization.diarize_correction import (
    CorrectionBundle,
    StaleDiarizationError,
    correction_uuid,
    load_corrected_record,
    write_correction_bundle,
)
from fakeyou.persistence.record import TranscriptEntry, load_meeting_record

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


def test_write_correction_bundle_rejects_hostile_speaker_label(tmp_path: Path) -> None:
    audio_dir, record_path = _session(tmp_path)
    hostile = DiarizedTurn(
        start_ms=0, end_ms=1000, speaker="A\nSPEAKER m1 1 0.000 1.000 <NA> <NA> X <NA>"
    )

    with pytest.raises(FakeYouError, match="speaker") as caught:
        write_correction_bundle(audio_dir, record_path, [hostile], clock=lambda: FIXED_NOW)
    assert caught.value.code is ErrorCode.DIARIZE_SPEAKER_LABEL_INVALID

    assert not (record_path.parent / "diarization.rttm").exists()
    assert not (record_path.parent / "diarization.json").exists()


def _transcript_line(index: int, capture_ms: float) -> str:
    return json.dumps(
        {
            "kind": "transcript",
            "at": "2026-10-08T00:00:00+00:00",
            "id": f"event-{index}",
            "spoken_at": "2026-10-08T00:00:00+00:00",
            "source": "meeting_audio",
            "text": "hello world",
            "participant_id": "unknown",
            "speaker_evidence_id": None,
            "stream_id": "",
            "capture_ms": capture_ms,
        }
    )


def _record_session(
    tmp_path: Path, captures: list[float], *, anchors: list[dict[str, Any]]
) -> tuple[Path, Path]:
    meeting_dir = tmp_path / "meetings" / "m1"
    meeting_dir.mkdir(parents=True)
    record_path = meeting_dir / "record.jsonl"
    record_path.write_text(
        "".join(_transcript_line(index, capture) + "\n" for index, capture in enumerate(captures)),
        encoding="utf-8",
    )
    audio_dir = meeting_dir / "audio"
    audio_dir.mkdir(parents=True)
    (audio_dir / "slots.json").write_text(
        json.dumps({"schema": "slot_audio/v2", "slots": {}}), encoding="utf-8"
    )
    (audio_dir / "anchors.jsonl").write_text(
        "".join(json.dumps(anchor) + "\n" for anchor in anchors), encoding="utf-8"
    )
    return audio_dir, record_path


def test_load_corrected_record_attaches_exact_uuids_with_anchor_offset(tmp_path: Path) -> None:
    anchors = [{"slot": 0, "seq": 0, "page_ms": 5000.0, "mono": 1.0}]
    audio_dir, record_path = _record_session(tmp_path, [6000.0, 10000.0], anchors=anchors)
    write_correction_bundle(audio_dir, record_path, _turns(), clock=lambda: FIXED_NOW)

    entries = load_corrected_record(record_path)
    transcripts = cast(list[TranscriptEntry], entries)

    assert [entry.corrected_speaker_uuid for entry in transcripts] == [
        _expected_uuid("A", 0),
        _expected_uuid("B", 3600),
    ]
    assert all(isinstance(entry, TranscriptEntry) for entry in entries)


def test_load_corrected_record_without_sidecar_matches_plain_load(tmp_path: Path) -> None:
    anchors = [{"slot": 0, "seq": 0, "page_ms": 5000.0, "mono": 1.0}]
    _, record_path = _record_session(tmp_path, [6000.0, 10000.0], anchors=anchors)

    assert load_corrected_record(record_path) == load_meeting_record(record_path)


def test_load_corrected_record_rejects_changed_record_as_stale(tmp_path: Path) -> None:
    anchors = [{"slot": 0, "seq": 0, "page_ms": 5000.0, "mono": 1.0}]
    audio_dir, record_path = _record_session(tmp_path, [6000.0], anchors=anchors)
    write_correction_bundle(audio_dir, record_path, _turns(), clock=lambda: FIXED_NOW)
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write(_transcript_line(9, 6100.0) + "\n")

    with pytest.raises(DiarizeError, match="stale") as error:
        load_corrected_record(record_path)

    assert error.value.code is ErrorCode.DIARIZE_STALE


def test_stale_error_is_distinguishable_from_invalid_sidecar(tmp_path: Path) -> None:
    assert issubclass(StaleDiarizationError, DiarizeError)
    anchors = [{"slot": 0, "seq": 0, "page_ms": 5000.0, "mono": 1.0}]
    audio_dir, record_path = _record_session(tmp_path, [6000.0], anchors=anchors)
    write_correction_bundle(audio_dir, record_path, _turns(), clock=lambda: FIXED_NOW)
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write(_transcript_line(9, 6100.0) + "\n")

    with pytest.raises(StaleDiarizationError, match="stale") as error:
        load_corrected_record(record_path)

    assert error.value.code is ErrorCode.DIARIZE_STALE


def test_load_corrected_record_leaves_far_entries_unattached(tmp_path: Path) -> None:
    _, record_path = _record_session(tmp_path, [500.0, 3000.0, 3001.0, 20000.0], anchors=[])
    audio_dir = record_path.parent / "audio"
    write_correction_bundle(
        audio_dir,
        record_path,
        [DiarizedTurn(start_ms=0, end_ms=1000, speaker="A")],
        clock=lambda: FIXED_NOW,
    )

    entries = load_corrected_record(record_path)
    transcripts = cast(list[TranscriptEntry], entries)

    assert [entry.corrected_speaker_uuid for entry in transcripts] == [
        _expected_uuid("A", 0),
        _expected_uuid("A", 0),
        None,
        None,
    ]


def test_old_record_lines_without_correction_field_still_load(tmp_path: Path) -> None:
    _, record_path = _record_session(tmp_path, [6000.0], anchors=[])
    assert "corrected_speaker_uuid" not in record_path.read_text(encoding="utf-8")

    entries = load_meeting_record(record_path)

    assert len(entries) == 1
    assert isinstance(entries[0], TranscriptEntry)
    assert entries[0].corrected_speaker_uuid is None
    assert load_corrected_record(record_path) == entries


def test_load_corrected_record_rejects_corrupt_anchors(tmp_path: Path) -> None:
    audio_dir, record_path = _record_session(
        tmp_path, [6000.0, 10000.0], anchors=[{"slot": 0, "seq": 0, "page_ms": 5000.0}]
    )
    write_correction_bundle(audio_dir, record_path, _turns(), clock=lambda: FIXED_NOW)
    valid = json.dumps({"slot": 0, "seq": 1, "page_ms": 5020.0})
    (audio_dir / "anchors.jsonl").write_text(f"{{corrupt\n{valid}\n", encoding="utf-8")

    with pytest.raises(DiarizeError, match="cannot be read") as error:
        load_corrected_record(record_path)

    assert error.value.code is ErrorCode.DIARIZE_INPUT_UNREADABLE


def test_load_corrected_record_without_anchors_applies_zero_offset(tmp_path: Path) -> None:
    audio_dir, record_path = _record_session(
        tmp_path, [1000.0, 4000.0], anchors=[{"slot": 0, "seq": 0, "page_ms": 5000.0}]
    )
    write_correction_bundle(audio_dir, record_path, _turns(), clock=lambda: FIXED_NOW)
    (audio_dir / "anchors.jsonl").unlink()

    entries = load_corrected_record(record_path)
    transcripts = cast(list[TranscriptEntry], entries)

    assert [entry.corrected_speaker_uuid for entry in transcripts] == [
        _expected_uuid("A", 0),
        _expected_uuid("B", 3600),
    ]
