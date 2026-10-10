import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fakeyou.catalog.errors import ErrorCode
from fakeyou.debrief.debrief_schema import (
    DebriefDraft,
    DebriefItem,
    DebriefValidationError,
    EvidenceRef,
    ObjectiveFinding,
)
from fakeyou.debrief.debrief_sources import (
    SourceEntry,
    chunk_sources,
    load_context_snapshot,
    load_record_snapshot,
    validate_draft,
    validate_evidence,
)
from fakeyou.diarization.diarize_client import DiarizedTurn
from fakeyou.diarization.diarize_correction import (
    JSON_FILE_NAME,
    correction_uuid,
    write_correction_bundle,
)
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal
from fakeyou.persistence.brief import BriefItem, MeetingBrief, MeetingContext, brief_digest

AT = datetime(2026, 10, 3, tzinfo=UTC)
PRIVATE_MARKER = "PRIVATE-TRANSCRIPT-MARKER"


def transcript(**changes: Any) -> dict[str, Any]:
    return {
        "kind": "transcript",
        "at": "2026-10-03T12:00:00Z",
        "id": "entry-1",
        "spoken_at": "12:00:00",
        "source": "caption",
        "text": "Please ask the owner about the new deadline.",
        "participant_id": "unknown",
        "speaker_evidence_id": None,
    } | changes


def encoded(*entries: dict[str, Any]) -> bytes:
    return ("\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n").encode()


def source(
    index: int = 1, text: str = "Please ask the owner about the new deadline."
) -> SourceEntry:
    return SourceEntry(
        id=f"transcript:entry-{index}",
        text=text,
        kind="participant",
        participant_id="unknown",
        at=AT,
    )


@pytest.fixture
def brief() -> MeetingBrief:
    return MeetingBrief(
        schema_version="meeting_brief/v1",
        owner_id="source-owner",
        twin_id="source-twin",
        meeting_id="source-meeting",
        objectives=(BriefItem(id="shared", text="Find blockers."),),
        questions=(BriefItem(id="shared", text="What is needed?"),),
        approved_facts=(BriefItem(id="fact", text="A venue exists."),),
    )


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
    with pytest.raises(FrozenInstanceError):
        snapshot.record_sha256 = "b" * 64
    with pytest.raises(FrozenInstanceError):
        snapshot.sources[0].participant_id = "guessed-owner"


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

    with pytest.raises(DebriefValidationError) as caught:
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

    with pytest.raises(DebriefValidationError) as caught:
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

    with pytest.raises(DebriefValidationError) as caught:
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
    with pytest.raises(DebriefValidationError) as caught:
        loader(tmp_path)

    assert caught.value.code is code


def test_missing_record_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DebriefValidationError) as caught:
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

    with pytest.raises(DebriefValidationError) as caught:
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

    with pytest.raises(DebriefValidationError) as caught:
        load_context_snapshot(path)

    assert caught.value.code is ErrorCode.DEBRIEF_CONTEXT_TOO_LARGE


def test_context_endless_file_is_bounded() -> None:
    with pytest.raises(DebriefValidationError) as caught:
        load_context_snapshot(Path("/dev/zero"))

    assert caught.value.code is ErrorCode.DEBRIEF_CONTEXT_TOO_LARGE


def test_sources_require_aware_clocks() -> None:
    with pytest.raises(DebriefValidationError) as caught:
        SourceEntry(
            id="transcript:x",
            text="Claim",
            kind="participant",
            participant_id="unknown",
            at=datetime(2026, 10, 3),
        )

    assert caught.value.code is ErrorCode.DEBRIEF_SOURCE_TIME_NOT_AWARE


def test_chunks_cover_each_source_with_default_overlap() -> None:
    sources = tuple(source(index, "x") for index in range(80))

    chunks = chunk_sources(sources)

    assert [len(chunk) for chunk in chunks] == [40, 40, 8]
    assert chunks[0][-4:] == chunks[1][:4]
    assert chunks[1][-4:] == chunks[2][:4]
    assert {entry.id for chunk in chunks for entry in chunk} == {entry.id for entry in sources}
    assert chunks == chunk_sources(sources)


def test_chunks_obey_character_limit_and_reduce_overlap_to_make_progress() -> None:
    sources = tuple(source(index, "x" * 8000) for index in range(5))

    chunks = chunk_sources(sources)

    assert [tuple(entry.id for entry in chunk) for chunk in chunks] == [
        ("transcript:entry-0", "transcript:entry-1"),
        ("transcript:entry-1", "transcript:entry-2"),
        ("transcript:entry-2", "transcript:entry-3"),
        ("transcript:entry-3", "transcript:entry-4"),
    ]
    assert all(sum(len(entry.text) for entry in chunk) <= 16000 for chunk in chunks)


def test_overlap_drops_when_whole_entry_fills_the_chunk() -> None:
    sources = (source(1, "x" * 16000), source(2, "y" * 16000))

    assert chunk_sources(sources) == ((sources[0],), (sources[1],))


def test_small_entry_limits_cannot_loop_on_overlap() -> None:
    sources = tuple(source(index, "x") for index in range(4))

    assert chunk_sources(sources, max_entries=1) == tuple((entry,) for entry in sources)
    assert chunk_sources(sources, max_entries=2, overlap=0) == (sources[:2], sources[2:])


def test_chunks_reject_oversize_entries() -> None:
    with pytest.raises(DebriefValidationError) as caught:
        chunk_sources((source(text="x" * 16001),))

    assert caught.value.code is ErrorCode.DEBRIEF_SOURCE_EXCEEDS_CHUNK


def test_twenty_chunks_are_accepted_but_twenty_one_are_rejected() -> None:
    sources = tuple(source(index, "x") for index in range(21))

    assert len(chunk_sources(sources[:20], max_entries=1, overlap=0)) == 20
    with pytest.raises(DebriefValidationError) as caught:
        chunk_sources(sources, max_entries=1, overlap=0)

    assert caught.value.code is ErrorCode.DEBRIEF_TOO_MANY_CHUNKS


def test_chunks_reject_empty_sources() -> None:
    with pytest.raises(DebriefValidationError) as caught:
        chunk_sources(())

    assert caught.value.code is ErrorCode.DEBRIEF_NO_SOURCES


@pytest.mark.parametrize(
    "limits",
    [
        {"max_entries": 0},
        {"max_entries": 41},
        {"max_characters": 0},
        {"max_characters": 16001},
        {"overlap": -1},
        {"overlap": 5},
    ],
)
def test_chunk_parameters_preserve_pilot_bounds(limits: dict[str, int]) -> None:
    with pytest.raises(DebriefValidationError) as caught:
        chunk_sources((source(),), **limits)

    assert caught.value.code is ErrorCode.DEBRIEF_CHUNK_LIMITS_INVALID


def test_exact_quote_keeps_original_whitespace() -> None:
    entry = source(text="Before  Friday\n\tafter.")
    reference = EvidenceRef(entry_id=entry.id, quote="  Friday\n\t")

    validate_evidence((reference,), {entry.id: entry})

    assert reference.quote == "  Friday\n\t"


@pytest.mark.parametrize(
    "reference",
    [
        EvidenceRef(entry_id="transcript:entry-1", quote="The owner approved Monday."),
        EvidenceRef(entry_id="transcript:missing", quote="Please ask"),
        EvidenceRef(entry_id="transcript:entry-1", quote="ask  the owner"),
        EvidenceRef(entry_id="transcript:entry-1", quote=" "),
        EvidenceRef(entry_id="transcript:entry-1", quote="please ask"),
    ],
)
def test_invalid_evidence_is_rejected(reference: EvidenceRef) -> None:
    entry = source()
    with pytest.raises(DebriefValidationError) as caught:
        validate_evidence((reference,), {entry.id: entry})

    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_INVALID


def test_missing_evidence_is_rejected() -> None:
    with pytest.raises(DebriefValidationError) as caught:
        validate_evidence((), {})

    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_MISSING


def test_draft_item_references_cannot_escape_the_supplied_chunk() -> None:
    entry = source()
    other = source(2)
    draft = DebriefDraft(
        items=(
            DebriefItem(
                kind="owner_decision",
                text="Ask about the deadline.",
                status="needs_owner",
                evidence=(EvidenceRef(entry_id=other.id, quote="Please ask"),),
            ),
        ),
        findings=(),
    )

    with pytest.raises(DebriefValidationError) as caught:
        validate_draft(draft, sources={entry.id: entry}, brief=None)

    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_INVALID
    validate_draft(draft, sources={other.id: other}, brief=None)


def test_draft_accepts_namespaced_objectives_and_questions(brief: MeetingBrief) -> None:
    entry = source()
    reference = EvidenceRef(entry_id=entry.id, quote="Please ask")
    draft = DebriefDraft(
        items=(),
        findings=(
            ObjectiveFinding(
                objective_id="objective:shared", status="addressed", evidence=(reference,)
            ),
            ObjectiveFinding(objective_id="question:shared", status="open", evidence=()),
        ),
    )

    validate_draft(draft, sources={entry.id: entry}, brief=brief)


def test_addressed_finding_rejects_twin_only_evidence(brief: MeetingBrief) -> None:
    twin = SourceEntry(
        id="twin_speech:2",
        text="I will move the deadline to Monday.",
        kind="twin",
        participant_id="twin",
        at=AT,
    )
    draft = DebriefDraft(
        items=(),
        findings=(
            ObjectiveFinding(
                objective_id="objective:shared",
                status="addressed",
                evidence=(EvidenceRef(entry_id=twin.id, quote="move the deadline"),),
            ),
        ),
    )

    with pytest.raises(DebriefValidationError) as caught:
        validate_draft(draft, sources={twin.id: twin}, brief=brief)

    assert caught.value.code is ErrorCode.DEBRIEF_ADDRESSED_NEEDS_PARTICIPANT


def test_addressed_finding_accepts_participant_evidence(brief: MeetingBrief) -> None:
    entry = source()
    twin = SourceEntry(id="twin_speech:2", text="Noted.", kind="twin", participant_id="twin", at=AT)
    draft = DebriefDraft(
        items=(),
        findings=(
            ObjectiveFinding(
                objective_id="objective:shared",
                status="addressed",
                evidence=(
                    EvidenceRef(entry_id=entry.id, quote="Please ask"),
                    EvidenceRef(entry_id=twin.id, quote="Noted"),
                ),
            ),
        ),
    )

    validate_draft(draft, sources={entry.id: entry, twin.id: twin}, brief=brief)


@pytest.mark.parametrize("finding_id", ["objective:missing", "question:missing", "objective:fact"])
def test_draft_rejects_non_brief_finding_ids(brief: MeetingBrief, finding_id: str) -> None:
    draft = DebriefDraft(
        items=(),
        findings=(ObjectiveFinding(objective_id=finding_id, status="uncertain", evidence=()),),
    )

    with pytest.raises(DebriefValidationError) as caught:
        validate_draft(draft, sources={}, brief=brief)

    assert caught.value.code is ErrorCode.DEBRIEF_FINDING_NOT_IN_BRIEF


def test_draft_without_brief_must_have_no_findings() -> None:
    draft = DebriefDraft(
        items=(),
        findings=(
            ObjectiveFinding(objective_id="objective:shared", status="uncertain", evidence=()),
        ),
    )

    with pytest.raises(DebriefValidationError) as caught:
        validate_draft(draft, sources={}, brief=None)

    assert caught.value.code is ErrorCode.DEBRIEF_FINDING_NOT_IN_BRIEF
    validate_draft(DebriefDraft(items=(), findings=()), sources={}, brief=None)


@pytest.mark.parametrize("status", ["addressed", "open", "uncertain"])
def test_finding_evidence_cannot_escape_chunk(brief: MeetingBrief, status: Any) -> None:
    draft = DebriefDraft(
        items=(),
        findings=(
            ObjectiveFinding(
                objective_id="question:shared",
                status=status,
                evidence=(EvidenceRef(entry_id="transcript:missing", quote="Claim"),),
            ),
        ),
    )

    with pytest.raises(DebriefValidationError) as caught:
        validate_draft(draft, sources={}, brief=brief)

    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_INVALID


def test_draft_status_validation_rejects_unvalidated_model_instances() -> None:
    entry = source()
    reference = EvidenceRef(entry_id=entry.id, quote="Please ask")
    invalid_item = DebriefItem.model_construct(
        kind="owner_decision", text="Ask the owner.", status="stated", evidence=(reference,)
    )
    draft = DebriefDraft.model_construct(items=(invalid_item,), findings=())

    with pytest.raises(DebriefValidationError) as caught:
        validate_draft(draft, sources={entry.id: entry}, brief=None)

    assert caught.value.code is ErrorCode.DEBRIEF_DRAFT_INVALID


def test_draft_finding_status_must_be_valid_even_without_evidence(brief: MeetingBrief) -> None:
    invalid = ObjectiveFinding.model_construct(
        objective_id="objective:shared", status="completed", evidence=()
    )
    draft = DebriefDraft.model_construct(items=(), findings=(invalid,))

    with pytest.raises(DebriefValidationError) as caught:
        validate_draft(draft, sources={}, brief=brief)

    assert caught.value.code is ErrorCode.DEBRIEF_DRAFT_INVALID


def test_evidence_map_cannot_alias_a_different_source_id() -> None:
    entry = source(2)
    reference = EvidenceRef(entry_id="transcript:entry-1", quote="Please ask")

    with pytest.raises(DebriefValidationError) as caught:
        validate_evidence((reference,), {reference.entry_id: entry})

    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_INVALID


@pytest.mark.parametrize("placeholder", ["null", "NULL", " none ", "unknown", "n/a", ""])
def test_draft_item_reads_placeholder_claims_as_absent(placeholder: str) -> None:
    item = DebriefItem(
        kind="action_item",
        text="Send the draft.",
        status="stated",
        evidence=(EvidenceRef(entry_id="transcript:t1", quote="Please ask"),),
        assignee_claim=placeholder,
        due_text=placeholder,
    )

    assert (item.assignee_claim, item.due_text) == (None, None)


@pytest.mark.parametrize(
    ("field", "value", "valid"),
    [
        ("assignee_claim", "Rani", False),
        ("due_text", "Friday", False),
        ("due_text", "ASK", True),
        ("assignee_claim", "please", True),
    ],
)
def test_draft_claims_must_come_from_the_item_quotes(field: str, value: str, valid: bool) -> None:
    entry = source()
    draft = DebriefDraft(
        items=(
            DebriefItem(
                kind="action_item",
                text="Ask about the deadline.",
                status="stated",
                evidence=(EvidenceRef(entry_id=entry.id, quote="Please ask"),),
                **{field: value},
            ),
        ),
        findings=(),
    )

    if valid:
        validate_draft(draft, sources={entry.id: entry}, brief=None)
    else:
        with pytest.raises(DebriefValidationError, match="claim") as caught:
            validate_draft(draft, sources={entry.id: entry}, brief=None)
        assert caught.value.code is ErrorCode.DEBRIEF_CLAIM_NOT_IN_QUOTES


@pytest.mark.parametrize(
    ("claim", "quotes", "valid"),
    [
        ("Monday.", ("send it Monday",), True),
        ("tomorrow  after lunch", ("send it tomorrow after lunch",), True),
        ("a", ("Please ask",), False),
        ("-", ("follow-up",), False),
        ("Please ask", ("Please", "ask"), False),
        ("ask", ("Please", "ask"), True),
    ],
)
def test_claim_grounding_is_per_quote_on_word_boundaries(
    claim: str, quotes: tuple[str, ...], valid: bool
) -> None:
    entry = source(text="Please ask: send it Monday, send it tomorrow after lunch, follow-up")
    draft = DebriefDraft(
        items=(
            DebriefItem(
                kind="action_item",
                text="Ask about the deadline.",
                status="stated",
                evidence=tuple(EvidenceRef(entry_id=entry.id, quote=quote) for quote in quotes),
                due_text=claim,
            ),
        ),
        findings=(),
    )

    if valid:
        validate_draft(draft, sources={entry.id: entry}, brief=None)
    else:
        with pytest.raises(DebriefValidationError, match="claim") as caught:
            validate_draft(draft, sources={entry.id: entry}, brief=None)
        assert caught.value.code is ErrorCode.DEBRIEF_CLAIM_NOT_IN_QUOTES


def test_corrected_sidecar_attributes_quotes_to_corrected_speaker(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript(), transcript(id="entry-2", capture_ms=5000.0)))
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        path,
        [
            DiarizedTurn(start_ms=0, end_ms=3400, speaker="A"),
            DiarizedTurn(start_ms=3600, end_ms=7200, speaker="B"),
        ],
        clock=lambda: AT,
    )

    snapshot = load_record_snapshot(path)

    assert [entry.id for entry in snapshot.sources] == [
        "transcript:entry-1",
        "transcript:entry-2",
    ]
    assert [entry.participant_id for entry in snapshot.sources] == [
        correction_uuid("A", 0),
        correction_uuid("B", 3600),
    ]
    assert snapshot.record_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_snapshot_without_sidecar_matches_live_label_baseline(tmp_path: Path) -> None:
    raw = encoded(transcript(), transcript(id="entry-2", participant_id="voice-a"))
    path = tmp_path / "record.jsonl"
    path.write_bytes(raw)

    snapshot = load_record_snapshot(path)

    assert snapshot.record_sha256 == hashlib.sha256(raw).hexdigest()
    assert snapshot.sources == (
        SourceEntry(
            id="transcript:entry-1",
            text="Please ask the owner about the new deadline.",
            kind="participant",
            participant_id="unknown",
            at=datetime(2026, 10, 3, 12, tzinfo=UTC),
        ),
        SourceEntry(
            id="transcript:entry-2",
            text="Please ask the owner about the new deadline.",
            kind="participant",
            participant_id="voice-a",
            at=datetime(2026, 10, 3, 12, tzinfo=UTC),
        ),
    )


def test_stale_sidecar_falls_back_to_plain_record_and_records_fault(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript()))
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: AT,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(transcript(id="entry-2")) + "\n")
    faults = FaultReporter(journal=Journal("meeting-1", emit=lambda line: None))

    snapshot = load_record_snapshot(path, faults=faults)

    assert [entry.participant_id for entry in snapshot.sources] == ["unknown", "unknown"]
    assert faults.total() == 1
    report = faults.report()
    assert report is not None
    kinds = report["faults"]["kinds"]
    assert kinds == [
        {
            "source": "meeting_record",
            "operation": None,
            "kind": "unknown",
            "count": 1,
            "status": None,
            "detail": "diarization sidecar is stale: record changed since correction",
        }
    ]


def test_stale_sidecar_with_no_fault_reporter_uses_live_labels(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript()))
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: AT,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(transcript(id="entry-2")) + "\n")

    snapshot = load_record_snapshot(path)

    assert [entry.participant_id for entry in snapshot.sources] == ["unknown", "unknown"]


@pytest.mark.parametrize("sidecar", ["unsupported_schema", "corrupt_bytes"])
def test_unusable_sidecar_falls_back_to_live_labels_and_records_fault(
    tmp_path: Path, sidecar: str
) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript()))
    if sidecar == "unsupported_schema":
        (path.parent / JSON_FILE_NAME).write_text(
            json.dumps({"schema": "diarization_correction/v9", "turns": []})
        )
    else:
        (path.parent / JSON_FILE_NAME).write_bytes(b"{invalid sidecar")
    faults = FaultReporter(journal=Journal("meeting-1", emit=lambda line: None))

    snapshot = load_record_snapshot(path, faults=faults)

    assert [entry.participant_id for entry in snapshot.sources] == ["unknown"]
    assert faults.total() == 1
    report = faults.report()
    assert report is not None
    kinds = report["faults"]["kinds"]
    expected_detail = {
        "unsupported_schema": "diarization sidecar schema is unsupported",
        "corrupt_bytes": "diarization sidecar is invalid",
    }[sidecar]
    assert kinds == [
        {
            "source": "meeting_record",
            "operation": None,
            "kind": "unknown",
            "count": 1,
            "status": None,
            "detail": expected_detail,
        }
    ]


def test_stale_sidecar_without_fault_reporter_emits_journal_fallback(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript()))
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: AT,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(transcript(id="entry-2")) + "\n")
    lines: list[str] = []
    journal = Journal("meeting-1", emit=lines.append)

    snapshot = load_record_snapshot(path, journal=journal)

    assert [entry.participant_id for entry in snapshot.sources] == ["unknown", "unknown"]
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "fault_recorded"
    assert row["source"] == "meeting_record"
    assert row["kind"] == "unknown"
    assert row["detail"] == "diarization sidecar is stale: record changed since correction"


def test_unexpected_correction_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript()))

    def boom(record_path: Path) -> list[Any]:
        raise RuntimeError("correction backend failed")

    monkeypatch.setattr("fakeyou.debrief.debrief_sources.load_corrected_record", boom)

    with pytest.raises(RuntimeError):
        load_record_snapshot(path)
