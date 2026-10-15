import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

import fakeyou.debrief.debrief as module
from fakeyou.catalog.errors import ErrorCode
from fakeyou.debrief.debrief_schema import DebriefValidationError, EvidenceSource, ObjectiveFinding
from fakeyou.persistence.brief import MeetingBrief, MeetingContext, brief_digest
from fakeyou.persistence.files import write_atomically
from tests._debrief_support import AT, item, ref, report_for, source
from tests._debrief_support import (
    brief as brief,
)
from tests._debrief_support import (
    record_path as record_path,
)
from tests._error_support import message_pattern


def test_publish_json_last_and_rebuild_markdown_from_canonical_report(
    record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = report_for(record_path)
    written: list[str] = []
    real_write = write_atomically

    def tracked(path: Path, data: bytes) -> None:
        written.append(path.name)
        real_write(path, data)

    monkeypatch.setattr(module, "write_atomically", tracked)
    module.write_debrief(record_path, report)
    assert written == ["debrief.md", "debrief.json"]
    assert module.read_debrief(record_path) == report
    markdown = record_path.parent / "debrief.md"
    markdown.unlink()
    module.rebuild_debrief_markdown(record_path)
    assert markdown.read_text() == module.render_debrief(report)


@pytest.mark.parametrize("failure_name", ["debrief.md", "debrief.json"])
def test_atomic_publish_failure_preserves_previous_canonical_report(
    record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_name: str,
) -> None:
    previous = report_for(record_path)
    module.write_debrief(record_path, previous)
    canonical = record_path.parent / "debrief.json"
    before = canonical.read_bytes()
    real_write = write_atomically

    def failing(path: Path, data: bytes) -> None:
        if path.name == failure_name:
            raise OSError("synthetic write failure")
        real_write(path, data)

    monkeypatch.setattr(module, "write_atomically", failing)
    with pytest.raises(OSError, match="synthetic write failure"):
        module.write_debrief(record_path, report_for(record_path, model="new-model"))
    assert canonical.read_bytes() == before
    assert module.debrief_report_status(record_path) == "available"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("record", ErrorCode.DEBRIEF_SOURCE_HASH_CHANGED),
        ("context", ErrorCode.DEBRIEF_CONTEXT_INVALID),
    ],
)
def test_publication_checks_freshness_before_replacing_previous_report(
    record_path: Path,
    change: str,
    code: ErrorCode,
) -> None:
    report = report_for(record_path)
    module.write_debrief(record_path, report)
    canonical = record_path.parent / "debrief.json"
    before = canonical.read_bytes()
    if change == "record":
        record_path.write_bytes(record_path.read_bytes() + b"\n")
    else:
        (record_path.parent / "meeting-context.json").write_text("{}")
    with pytest.raises(DebriefValidationError, match=message_pattern(code)) as caught:
        module.write_debrief(record_path, report)
    assert caught.value.code is code
    assert canonical.read_bytes() == before


def test_publication_rejects_fabricated_source_metadata(record_path: Path) -> None:
    report = report_for(record_path)
    wrong = report.evidence_sources[0].model_copy(update={"participant_id": "owner"})
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_EVIDENCE_METADATA_DIFFERS)
    ) as caught:
        module.write_debrief(record_path, report.model_copy(update={"evidence_sources": (wrong,)}))
    assert caught.value.code is ErrorCode.DEBRIEF_EVIDENCE_METADATA_DIFFERS
    assert not (record_path.parent / "debrief.json").exists()


def test_report_status_checks_same_byte_record_and_context_hashes(
    record_path: Path,
    brief: MeetingBrief,
) -> None:
    context = MeetingContext(
        schema_version="meeting_context/v1",
        brief=brief,
        brief_sha256=brief_digest(brief),
        persona_sha256="b" * 64,
    )
    context_path = record_path.parent / "meeting-context.json"
    context_path.write_text(context.model_dump_json())
    report = report_for(
        record_path,
        context_sha256=hashlib.sha256(context_path.read_bytes()).hexdigest(),
        findings=[
            ObjectiveFinding(objective_id="objective:o1", status="uncertain", evidence=()),
            ObjectiveFinding(objective_id="question:q1", status="uncertain", evidence=()),
        ],
    )
    assert module.debrief_report_status(record_path) == "missing"
    module.write_debrief(record_path, report)
    assert module.debrief_report_status(record_path) == "available"
    context_path.write_bytes(context_path.read_bytes() + b"\n")
    assert module.debrief_report_status(record_path) == "stale"
    context_path.unlink()
    assert module.debrief_report_status(record_path) == "stale"


@pytest.mark.parametrize("raw", ["partial", "{}", '{"schema_version":"meeting_debrief/v99"}'])
def test_corrupt_canonical_report_is_invalid(record_path: Path, raw: str) -> None:
    (record_path.parent / "debrief.json").write_text(raw)
    assert module.debrief_report_status(record_path) == "invalid"


@pytest.mark.parametrize("previous_exists", [True, False])
def test_post_replace_write_failure_restores_previous_canonical_state(
    record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    previous_exists: bool,
) -> None:
    canonical = record_path.parent / "debrief.json"
    previous = report_for(record_path)
    if previous_exists:
        module.write_debrief(record_path, previous)
    before = canonical.read_bytes() if previous_exists else None
    real_write = write_atomically

    def failing_after_replace(path: Path, data: bytes) -> None:
        real_write(path, data)
        if path.name == "debrief.json":
            raise OSError("synthetic post-replace directory sync failure")

    monkeypatch.setattr(module, "write_atomically", failing_after_replace)
    with pytest.raises(OSError, match="synthetic post-replace directory sync failure"):
        module.write_debrief(record_path, report_for(record_path, model="new-model"))
    assert (canonical.read_bytes() if canonical.exists() else None) == before


def test_failed_rollback_preserves_old_backup_and_blocks_cache(
    record_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous = report_for(record_path)
    module.write_debrief(record_path, previous)
    canonical = record_path.parent / "debrief.json"
    before = canonical.read_bytes()
    real_write = write_atomically
    real_replace = os.replace

    def failing_write(path: Path, data: bytes) -> None:
        real_write(path, data)
        if path.name == "debrief.json":
            raise OSError("directory sync failed")

    def failing_restore(source: Path, target: Path) -> None:
        if str(source).endswith(".rollback"):
            raise OSError("rollback failed")
        real_replace(source, target)

    monkeypatch.setattr(module, "write_atomically", failing_write)
    monkeypatch.setattr(os, "replace", failing_restore)
    with pytest.raises(OSError, match="rollback failed"):
        module.write_debrief(record_path, report_for(record_path, model="failed-model"))
    backups = list(record_path.parent.glob("debrief.json.*.rollback"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before
    assert module.debrief_report_status(record_path) == "invalid"
    with pytest.raises(DebriefValidationError, match="recovery") as caught:
        module.read_debrief(record_path)
    assert caught.value.code is ErrorCode.DEBRIEF_PUBLICATION_RECOVERY
    monkeypatch.setattr(module, "write_atomically", real_write)
    with pytest.raises(DebriefValidationError, match="recovery") as caught:
        module.write_debrief(record_path, previous)
    assert caught.value.code is ErrorCode.DEBRIEF_PUBLICATION_RECOVERY
    assert backups[0].read_bytes() == before


@pytest.mark.parametrize(
    ("field", "change"),
    [
        ("items", (item(evidence=(ref(quote="Fabricated approval"),)),)),
        (
            "evidence_sources",
            (
                EvidenceSource(
                    id="transcript:e1",
                    text=source().text,
                    kind="participant",
                    participant_id="guessed-owner",
                    at=AT,
                ),
            ),
        ),
        ("evidence_sources", ()),
    ],
)
def test_cached_report_with_tampered_evidence_is_invalid(
    record_path: Path,
    field: str,
    change: Any,
) -> None:
    report = report_for(record_path)
    module.write_debrief(record_path, report)
    altered = report.model_copy(update={field: change})
    (record_path.parent / "debrief.json").write_text(altered.model_dump_json())
    assert module.debrief_report_status(record_path) == "invalid"


def test_publish_rejects_source_changed_after_markdown_write(
    record_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = report_for(record_path)
    module.write_debrief(record_path, previous)
    canonical = record_path.parent / "debrief.json"
    before = canonical.read_bytes()
    real_write = write_atomically

    def mutate_source(path: Path, data: bytes) -> None:
        real_write(path, data)
        if path.name == "debrief.md":
            record_path.write_bytes(record_path.read_bytes() + b"\n")

    monkeypatch.setattr(module, "write_atomically", mutate_source)
    with pytest.raises(
        DebriefValidationError, match=message_pattern(ErrorCode.DEBRIEF_SOURCE_HASH_CHANGED)
    ) as caught:
        module.write_debrief(record_path, report_for(record_path, model="new-model"))
    assert caught.value.code is ErrorCode.DEBRIEF_SOURCE_HASH_CHANGED
    assert canonical.read_bytes() == before


def test_publish_revalidates_bypassed_report_schema(record_path: Path) -> None:
    report = report_for(record_path).model_copy(update={"review_status": "verified"})
    with pytest.raises(ValueError, match="Input should be 'unreviewed'"):
        module.write_debrief(record_path, report)
    assert not (record_path.parent / "debrief.json").exists()


def test_cached_report_scope_must_match_historical_brief(
    record_path: Path,
    brief: MeetingBrief,
) -> None:
    context = MeetingContext(
        schema_version="meeting_context/v1",
        brief=brief,
        brief_sha256=brief_digest(brief),
        persona_sha256="b" * 64,
    )
    context_path = record_path.parent / "meeting-context.json"
    context_path.write_text(context.model_dump_json())
    report = report_for(
        record_path,
        context_sha256=hashlib.sha256(context_path.read_bytes()).hexdigest(),
        owner_id="different-owner",
        findings=[
            ObjectiveFinding(objective_id="objective:o1", status="uncertain", evidence=()),
            ObjectiveFinding(objective_id="question:q1", status="uncertain", evidence=()),
        ],
    )
    (record_path.parent / "debrief.json").write_text(report.model_dump_json())
    assert module.debrief_report_status(record_path) == "invalid"
