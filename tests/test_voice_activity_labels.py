import json
from pathlib import Path

from fakeyou.audio import voice_activity
from fakeyou.audio.probe_replay import (
    LABEL_STABILITY_GATE,
    OVERLAP_UNKNOWN_GATE,
    SpeakerAliases,
    replay_voice_labels,
    voice_fixture_from_probe,
)
from fakeyou.audio.voice_activity import voice_label, voice_speaker_name
from fakeyou.config.settings import DEFAULTS
from fakeyou.memory.ingestion import extraction_instructions, meeting_windows
from fakeyou.observability.report import build_session_report
from fakeyou.persistence.record import TranscriptEntry

FIXTURE = Path(__file__).parent / "fixtures" / "voice-labels-probe-attr-8.json"


CSRC_FIXTURE = Path(__file__).parent / "fixtures" / "voice-labels-csrc-live-3.json"


def test_voice_labels_run_past_z() -> None:
    assert [voice_label(index) for index in (0, 1, 25, 26, 27)] == ["A", "B", "Z", "AA", "AB"]
    assert voice_speaker_name("speaker-ab") == "Speaker AB"
    assert voice_speaker_name("unknown") is None
    assert voice_speaker_name("speaker-") is None
    assert voice_speaker_name("Rani") is None


def test_voice_speaker_name_renders_display_claim_as_unverified() -> None:
    assert voice_speaker_name("speaker-b", "Maya") == "Speaker B, shown as Maya, unverified"


def test_voice_speaker_name_without_claim_is_unchanged() -> None:
    assert voice_speaker_name("speaker-b", None) == "Speaker B"
    assert voice_speaker_name("speaker-b") == "Speaker B"


def test_voice_speaker_name_has_no_verified_path() -> None:
    assert voice_speaker_name("speaker-b", "Maya") != "Maya"
    assert not hasattr(voice_activity, "verified_name")


def test_extraction_instructions_explain_voice_labels_only_when_present() -> None:
    assert "anonymous voice labels" not in extraction_instructions("Rani Twin", ())
    assert "anonymous voice labels" in extraction_instructions("Rani Twin", (), voice_labels=True)


def test_voice_labels_default_off() -> None:
    assert DEFAULTS.conversation.voice_labels_enabled is False


def test_ingestion_names_voice_labels_as_unverified() -> None:
    entry = TranscriptEntry.model_validate(
        {
            "at": "2026-10-05T10:00:00+00:00",
            "id": "t1",
            "spoken_at": "2026-10-05T10:00:00+00:00",
            "source": "meeting_audio",
            "text": "The budget is fine.",
            "participant_id": "speaker-b",
            "speaker_evidence_id": "voice-2",
        }
    )
    [window] = meeting_windows([entry])
    assert window[0].speaker == "Speaker B with unverified identity"


def test_report_counts_voice_unknown_reasons() -> None:
    rows = [
        {
            "at": "2026-10-05T10:00:00+00:00",
            "event": "transcript_finalized",
            "meeting_id": "m1",
            "source": "stt",
            "transcript_id": transcript_id,
            "participant_id": participant_id,
            "speaker_evidence_id": evidence_id,
            "speaker_reason": reason,
        }
        for transcript_id, participant_id, evidence_id, reason in (
            ("t1", "speaker-a", "voice-1", "dominant_slot"),
            ("t2", "unknown", "voice-2", "overlap"),
            ("t3", "unknown", "voice-3", "overlap"),
        )
    ]
    participation = build_session_report(rows)["participation"]
    assert participation["attributed_utterances"] == 1
    assert participation["unknown_reasons"] == {"overlap": 2}


def test_voice_label_replay_gate_on_probe_attr_8() -> None:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    summary = replay_voice_labels(fixture)
    assert summary["solo_turns"] == 36
    assert summary["label_stability"] >= LABEL_STABILITY_GATE
    assert summary["overlap_unknown_rate"] >= OVERLAP_UNKNOWN_GATE
    assert summary["coverage"] >= 0.9
    assert summary["purity"] >= LABEL_STABILITY_GATE
    join_labels = summary["join_labels"]
    assert sorted(join_labels) == ["Guest 1", "Guest 2", "Guest 3", "Host"]
    assert len(set(join_labels.values())) == len(join_labels)


def test_voice_fixture_carries_encoded_frames(tmp_path: Path) -> None:
    probe = tmp_path / "probe.jsonl"
    probe.write_text(
        json.dumps(
            {
                "kind": "page",
                "page_ms": 1000.0,
                "mono_ms": 5000.0,
                "wall": "2026-10-05T10:00:00+00:00",
                "levels": [],
                "frames": [
                    {"page_ms": 900.0, "receiver": 1, "level": 0.4, "csrc": [42, 7]},
                    {"page_ms": 950.0, "receiver": 1},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record = tmp_path / "record.jsonl"
    record.write_text("", encoding="utf-8")
    truth = tmp_path / "truth.json"
    truth.write_text("[]", encoding="utf-8")
    fixture = voice_fixture_from_probe(
        probe, record, truth, twin_name="Twin", speakers=SpeakerAliases({}, {})
    )
    assert fixture["events"][0]["batch"]["frames"] == [[900.0, 1, 0.4, [42, 7]]]


def test_csrc_label_replay_gate_on_live_session() -> None:
    fixture = json.loads(CSRC_FIXTURE.read_text(encoding="utf-8"))
    summary = replay_voice_labels(fixture)
    assert summary["solo_turns"] == 11
    assert summary["coverage"] >= 0.9
    assert summary["label_stability"] >= LABEL_STABILITY_GATE
    assert summary["purity"] >= LABEL_STABILITY_GATE
    assert summary["overlap_unknown_rate"] >= OVERLAP_UNKNOWN_GATE
    assert summary["person_labels"] == {"A": ["speaker-a"], "B": ["speaker-b"]}
    assert set(summary["reasons"]) == {"dominant_csrc"}
