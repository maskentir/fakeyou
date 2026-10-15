from pathlib import Path

import pytest

import fakeyou.debrief.debrief as module
from fakeyou.diarization.correction_labels import correction_speaker_name
from fakeyou.diarization.diarize_correction import correction_uuid
from tests._debrief_support import FIXTURE, item, report_for
from tests._debrief_support import (
    record_path as record_path,
)


def test_render_preserves_source_metadata_and_escapes_participant_markup(record_path: Path) -> None:
    report = report_for(
        record_path,
        items=[
            item(
                text="[contact](javascript:alert(1)) <script>x</script>",
                assignee_claim="Someone",
                due_text="tomorrow",
            )
        ],
    )
    rendered = module.render_debrief(report)
    assert "unknown" in rendered
    assert "unreviewed" in rendered
    assert "needs_owner" in rendered
    assert report.record_sha256 in rendered
    assert "tomorrow" in rendered
    assert "<script>" not in rendered
    assert "[contact](javascript:" not in rendered
    assert "2026" in rendered


def test_renderer_distinguishes_no_findings_from_missing_brief(record_path: Path) -> None:
    report = report_for(record_path, context_sha256="b" * 64)
    assert "not_available" not in module.render_debrief(report)


@pytest.mark.parametrize("label", [FIXTURE["render_shown"], None])
def test_render_correction_uuid_uses_human_readable_unverified_speaker(
    record_path: Path, label: str | None
) -> None:
    uuid = correction_uuid("A", 0)
    renamed = (
        report_for(record_path).evidence_sources[0].model_copy(update={"participant_id": uuid})
    )
    report = report_for(record_path).model_copy(update={"evidence_sources": (renamed,)})
    rendered = module.render_debrief(
        report, speaker_names={uuid: correction_speaker_name("A", label)}
    )

    assert report.evidence_sources[0].participant_id == uuid
    if label is None:
        assert "Speaker A, unverified" in rendered
        assert "shown as" not in rendered
    else:
        assert f"Speaker A, shown as {label}, unverified" in rendered
