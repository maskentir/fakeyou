import json
from pathlib import Path

from fakeyou.diarization.correction_labels import (
    CorrectionTurn,
    LabelEvidence,
    build_correction_labels,
    describe_correction_speakers,
    write_correction_labels,
)
from fakeyou.diarization.diarize_correction import correction_uuid
from fakeyou.persistence.layout import CORRECTION_LABELS_FILE_NAME, DIARIZATION_JSON_FILE_NAME

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "correction_labels.json").read_text(encoding="utf-8")
)


def turn(speaker: str, start_ms: int, end_ms: int) -> CorrectionTurn:
    return CorrectionTurn(
        corrected_uuid=correction_uuid(speaker, start_ms),
        speaker=speaker,
        start_ms=float(start_ms),
        end_ms=float(end_ms),
    )


def evidence(name: str, start_ms: int, end_ms: int) -> LabelEvidence:
    return LabelEvidence(name=name, start_ms=float(start_ms), end_ms=float(end_ms))


def test_bound_uuid_keeps_first_name_when_later_evidence_arrives() -> None:
    single = turn("A", 0, 2000)
    first = build_correction_labels([single], [evidence(FIXTURE["bind_first"], 0, 2000)])
    second = build_correction_labels(
        [single],
        [evidence(FIXTURE["bind_first"], 0, 2000), evidence(FIXTURE["bind_later"], 0, 2000)],
        previous=first,
    )

    assert first == {single.corrected_uuid: FIXTURE["bind_first"]}
    assert second == first


def test_two_uuids_claiming_one_name_both_bind_null() -> None:
    first_turn = turn("A", 0, 2000)
    second_turn = turn("B", 3600, 7200)
    labels = build_correction_labels(
        [first_turn, second_turn],
        [
            evidence(FIXTURE["duplicate"], 0, 2000),
            evidence(FIXTURE["duplicate"], 3600, 7200),
        ],
    )

    assert labels == {first_turn.corrected_uuid: None, second_turn.corrected_uuid: None}


def test_rejoined_speaker_uuid_binds_a_new_label() -> None:
    before = turn("A", 0, 2000)
    after = turn("A", 5000, 7000)
    assert before.corrected_uuid != after.corrected_uuid
    first = build_correction_labels([before], [evidence(FIXTURE["rejoin_before"], 0, 2000)])
    labels = build_correction_labels(
        [before, after],
        [
            evidence(FIXTURE["rejoin_before"], 0, 2000),
            evidence(FIXTURE["rejoin_after"], 5000, 7000),
        ],
        previous=first,
    )

    assert labels == {
        before.corrected_uuid: FIXTURE["rejoin_before"],
        after.corrected_uuid: FIXTURE["rejoin_after"],
    }


def test_label_fixtures_use_only_anonymous_names() -> None:
    assert set(FIXTURE["allowed"]) == {"Host", "Guest", "Twin"}
    names = {value for key, value in FIXTURE.items() if key != "allowed"}

    assert names <= set(FIXTURE["allowed"])


def test_describe_correction_speakers_skips_a_non_dict_turn(tmp_path: Path) -> None:
    record_path = tmp_path / "meeting" / "record.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    first = correction_uuid("A", 0)
    second = correction_uuid("B", 5000)
    turns: list[object] = [
        {"corrected_uuid": first, "speaker": "A"},
        "bogus",
        {"corrected_uuid": second, "speaker": "B"},
    ]
    (record_path.parent / DIARIZATION_JSON_FILE_NAME).write_text(
        json.dumps({"turns": turns}), encoding="utf-8"
    )
    write_correction_labels(
        record_path, {first: FIXTURE["rejoin_before"], second: FIXTURE["rejoin_after"]}
    )

    assert describe_correction_speakers(record_path) == {
        first: f"Speaker A, shown as {FIXTURE['rejoin_before']}, unverified",
        second: f"Speaker B, shown as {FIXTURE['rejoin_after']}, unverified",
    }


def test_one_speaker_with_several_turns_keeps_its_name_on_every_turn() -> None:
    first_turn = turn("A", 0, 5000)
    other_speaker = turn("B", 6000, 12000)
    second_turn = turn("A", 13000, 20000)
    labels = build_correction_labels(
        [first_turn, other_speaker, second_turn],
        [
            evidence(FIXTURE["bind_first"], 1000, 1000),
            evidence(FIXTURE["duplicate"], 7000, 7000),
            evidence(FIXTURE["bind_first"], 15000, 15000),
        ],
    )

    assert labels == {
        first_turn.corrected_uuid: FIXTURE["bind_first"],
        other_speaker.corrected_uuid: FIXTURE["duplicate"],
        second_turn.corrected_uuid: FIXTURE["bind_first"],
    }


def test_rerun_with_shifted_turn_ids_does_not_null_a_bound_speaker() -> None:
    before = turn("A", 0, 5000)
    first = build_correction_labels([before], [evidence(FIXTURE["bind_first"], 1000, 1000)])
    shifted = turn("A", 40, 5040)
    labels = build_correction_labels(
        [shifted],
        [evidence(FIXTURE["bind_first"], 1000, 1000)],
        previous=first,
    )

    assert labels == {shifted.corrected_uuid: FIXTURE["bind_first"]}


def test_stale_previous_uuids_do_not_count_against_current_speakers() -> None:
    stale = turn("B", 0, 1000)
    previous = {stale.corrected_uuid: FIXTURE["duplicate"]}
    current = turn("A", 5000, 7000)
    labels = build_correction_labels(
        [current],
        [evidence(FIXTURE["duplicate"], 5000, 7000)],
        previous=previous,
    )

    assert labels == {current.corrected_uuid: FIXTURE["duplicate"]}


def test_correction_digest_changes_when_only_the_labels_change(tmp_path: Path) -> None:
    from fakeyou.diarization.diarize_correction import correction_digest

    record_path = tmp_path / "meeting" / "record.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    (record_path.parent / DIARIZATION_JSON_FILE_NAME).write_text('{"turns": []}', encoding="utf-8")
    before = correction_digest(record_path)

    write_correction_labels(record_path, {correction_uuid("A", 0): FIXTURE["bind_first"]})

    assert correction_digest(record_path) != before


def test_correction_digest_is_none_without_a_sidecar(tmp_path: Path) -> None:
    from fakeyou.diarization.diarize_correction import correction_digest

    record_path = tmp_path / "meeting" / "record.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    write_correction_labels(record_path, {correction_uuid("A", 0): FIXTURE["bind_first"]})

    assert correction_digest(record_path) is None


def test_unreadable_labels_leave_the_digest_unknown(tmp_path: Path) -> None:
    from fakeyou.diarization.diarize_correction import correction_digest

    record_path = tmp_path / "meeting" / "record.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    (record_path.parent / DIARIZATION_JSON_FILE_NAME).write_text('{"turns": []}', encoding="utf-8")
    (record_path.parent / CORRECTION_LABELS_FILE_NAME).mkdir()

    assert correction_digest(record_path) is None
