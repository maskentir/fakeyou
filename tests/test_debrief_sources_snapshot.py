import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from fakeyou.catalog.errors import ErrorCode
from fakeyou.debrief.debrief_schema import DebriefValidationError
from fakeyou.debrief.debrief_sources import SourceEntry, load_context_snapshot, load_record_snapshot
from fakeyou.persistence.brief import MeetingBrief, MeetingContext, brief_digest
from tests._debrief_sources_support import (
    brief as brief,
)
from tests._debrief_sources_support import encoded, transcript
from tests._error_support import message_pattern

PRIVATE_MARKER = "PRIVATE-TRANSCRIPT-MARKER"


def context_bytes(brief: MeetingBrief) -> bytes:
    context = MeetingContext(
        schema_version="meeting_context/v1",
        brief=brief,
        brief_sha256=brief_digest(brief),
        persona_sha256="a" * 64,
    )
    return ("\n" + context.model_dump_json(indent=2) + "\n  ").encode()


def test_record_hash_matches_original_bytes_and_preserves_unknown_metadata(tmp_path: Path) -> None:
    raw = b"\n" + encoded(transcript(text="  Caf\u00e9\tdeadline.  ")).replace(b"\n", b"\r\n")
    path = tmp_path / "record.jsonl"
    path.write_bytes(raw)

    snapshot = load_record_snapshot(path)

    assert snapshot.record_sha256 == hashlib.sha256(raw).hexdigest()
    assert snapshot.sources == (
        SourceEntry(
            id="transcript:entry-1",
            text="  Caf\u00e9\tdeadline.  ",
            kind="participant",
            participant_id="unknown",
            at=datetime(2026, 10, 3, 12, tzinfo=UTC),
        ),
    )
    assert path.read_bytes() == raw
    snapshot_any = cast(Any, snapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot_any.record_sha256 = "b" * 64
    with pytest.raises(FrozenInstanceError):
        snapshot_any.sources[0].participant_id = "guessed-owner"


def test_record_snapshot_reads_once_even_if_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.jsonl"
    original = encoded(transcript())
    changed = encoded(transcript(text="Changed source."))
    path.write_bytes(original)
    read_bytes = Path.read_bytes
    reads = 0

    def replace_after_read(target: Path) -> bytes:
        nonlocal reads
        reads += 1
        raw = read_bytes(target)
        target.write_bytes(changed)
        return raw

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    snapshot = load_record_snapshot(path)

    assert reads == 1
    assert snapshot.record_sha256 == hashlib.sha256(original).hexdigest()
    assert snapshot.sources[0].text == "Please ask the owner about the new deadline."


def test_twin_ids_count_all_raw_lines_and_tool_output_is_not_a_source(tmp_path: Path) -> None:
    twin = {"kind": "twin_speech", "at": "2026-10-03T12:00:01Z", "text": "I will ask the owner."}
    tool = {
        "kind": "tool_result",
        "at": "2026-10-03T12:00:02Z",
        "tool": "lookup",
        "output": "This is not spoken evidence.",
        "duration_ms": 1.0,
    }
    path = tmp_path / "record.jsonl"
    path.write_bytes(b"\n" + encoded(transcript()) + b" \t\n" + encoded(tool, twin))

    sources = load_record_snapshot(path).sources

    assert [entry.id for entry in sources] == ["transcript:entry-1", "twin_speech:5"]
    assert sources[1].kind == "twin"
    assert sources[1].participant_id == "twin"
    assert sources[1].at == datetime(2026, 10, 3, 12, 0, 1, tzinfo=UTC)
    assert sources[1].text == "I will ask the owner."


def test_identical_transcript_ids_are_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript(), transcript(), transcript(id="entry-2")))

    assert [entry.id for entry in load_record_snapshot(path).sources] == [
        "transcript:entry-1",
        "transcript:entry-2",
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"text": PRIVATE_MARKER},
        {"at": "2026-10-03T12:01:00Z"},
        {"participant_id": PRIVATE_MARKER},
        {"spoken_at": "12:01:00"},
        {"source": PRIVATE_MARKER},
        {"speaker_evidence_id": PRIVATE_MARKER},
    ],
)
def test_contradictory_duplicate_ids_are_rejected_without_input(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript(), transcript(**changes)))

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_TRANSCRIPT_CONTRADICTORY)
    ) as caught:
        load_record_snapshot(path)

    assert caught.value.code is ErrorCode.DEBRIEF_TRANSCRIPT_CONTRADICTORY
    assert PRIVATE_MARKER not in str(caught.value)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"text":"PRIVATE-TRANSCRIPT-MARKER"', ErrorCode.DEBRIEF_RECORD_INVALID_LINE),
        (b"\xff\n", ErrorCode.DEBRIEF_RECORD_INVALID_LINE),
        (encoded(transcript(kind=PRIVATE_MARKER)), ErrorCode.DEBRIEF_RECORD_INVALID_LINE),
        (
            encoded(transcript(at="2026-10-03T12:00:00", text=PRIVATE_MARKER)),
            ErrorCode.DEBRIEF_RECORD_INVALID_LINE,
        ),
        (encoded(transcript(id="", text=PRIVATE_MARKER)), ErrorCode.DEBRIEF_TRANSCRIPT_ID_BLANK),
        (encoded(transcript(extra=PRIVATE_MARKER)), ErrorCode.DEBRIEF_RECORD_INVALID_LINE),
    ],
)
def test_corrupt_record_is_wrapped_without_transcript_input(
    tmp_path: Path, raw: bytes, code: ErrorCode
) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(raw)

    with pytest.raises(DebriefValidationError, match=message_pattern(code)) as caught:
        load_record_snapshot(path)

    assert caught.value.code is code
    assert PRIVATE_MARKER not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\n \t\r\n",
        encoded(transcript(text=" \t ")),
        encoded({"kind": "twin_speech", "at": "2026-10-03T12:00:00Z", "text": "  "}),
        encoded(
            {
                "kind": "tool_result",
                "at": "2026-10-03T12:00:00Z",
                "tool": "lookup",
                "output": PRIVATE_MARKER,
                "duration_ms": 1.0,
            }
        ),
    ],
)
def test_empty_or_unusable_records_are_rejected(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(raw)

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_NO_SPEECH_SOURCES)
    ) as caught:
        load_record_snapshot(path)

    assert caught.value.code is ErrorCode.DEBRIEF_NO_SPEECH_SOURCES


@pytest.mark.parametrize(
    ("loader", "code"),
    [
        (load_record_snapshot, ErrorCode.DEBRIEF_RECORD_UNREADABLE),
        (load_context_snapshot, ErrorCode.DEBRIEF_CONTEXT_UNREADABLE),
    ],
)
def test_source_os_errors_are_wrapped(tmp_path: Path, loader: Any, code: ErrorCode) -> None:
    with pytest.raises(DebriefValidationError, match=message_pattern(code)) as caught:
        loader(tmp_path)

    assert caught.value.code is code


def test_missing_record_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_RECORD_UNREADABLE)
    ) as caught:
        load_record_snapshot(tmp_path / "absent.jsonl")

    assert caught.value.code is ErrorCode.DEBRIEF_RECORD_UNREADABLE


def test_context_hash_is_for_the_original_context_bytes(
    tmp_path: Path, brief: MeetingBrief
) -> None:
    raw = context_bytes(brief)
    path = tmp_path / "meeting-context.json"
    path.write_bytes(raw)

    context, digest = load_context_snapshot(path)

    assert context is not None
    assert context.brief == brief
    assert digest == hashlib.sha256(raw).hexdigest()
    assert digest != brief_digest(brief)
    assert digest != context.persona_sha256


def test_missing_context_is_not_an_error(tmp_path: Path) -> None:
    assert load_context_snapshot(tmp_path / "absent.json") == (None, None)


@pytest.mark.parametrize("corruption", ["partial", "tampered", "empty", "extra"])
def test_corrupt_context_is_rejected_without_input(
    tmp_path: Path, brief: MeetingBrief, corruption: str
) -> None:
    raw = context_bytes(brief)
    data = json.loads(raw)
    if corruption == "partial":
        raw = b'{"private":"PRIVATE-TRANSCRIPT-MARKER"'
    elif corruption == "tampered":
        data["brief"]["objectives"][0]["text"] = PRIVATE_MARKER
        raw = json.dumps(data).encode()
    elif corruption == "empty":
        raw = b""
    else:
        data["private"] = PRIVATE_MARKER
        raw = json.dumps(data).encode()
    path = tmp_path / "meeting-context.json"
    path.write_bytes(raw)

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_CONTEXT_INVALID)
    ) as caught:
        load_context_snapshot(path)

    assert caught.value.code is ErrorCode.DEBRIEF_CONTEXT_INVALID
    assert PRIVATE_MARKER not in str(caught.value)
    assert caught.value.__suppress_context__


def test_context_ceiling_accepts_bound_and_rejects_overflow(
    tmp_path: Path, brief: MeetingBrief
) -> None:
    path = tmp_path / "meeting-context.json"
    raw = context_bytes(brief)
    padded = raw + b" " * (1024 * 1024 - len(raw))
    path.write_bytes(padded)
    assert load_context_snapshot(path)[1] == hashlib.sha256(padded).hexdigest()
    path.write_bytes(padded + b" ")

    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_CONTEXT_TOO_LARGE)
    ) as caught:
        load_context_snapshot(path)

    assert caught.value.code is ErrorCode.DEBRIEF_CONTEXT_TOO_LARGE


def test_context_endless_file_is_bounded() -> None:
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_CONTEXT_TOO_LARGE)
    ) as caught:
        load_context_snapshot(Path("/dev/zero"))

    assert caught.value.code is ErrorCode.DEBRIEF_CONTEXT_TOO_LARGE


def test_sources_require_aware_clocks() -> None:
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_SOURCE_TIME_NOT_AWARE)
    ) as caught:
        SourceEntry(
            id="transcript:x",
            text="Claim",
            kind="participant",
            participant_id="unknown",
            at=datetime(2026, 10, 3),
        )

    assert caught.value.code is ErrorCode.DEBRIEF_SOURCE_TIME_NOT_AWARE
