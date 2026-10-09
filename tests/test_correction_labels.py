import json
from pathlib import Path

from fakeyou.correction_labels import (
    CorrectionTurn,
    LabelEvidence,
    build_correction_labels,
    describe_correction_speakers,
    write_correction_labels,
)
from fakeyou.diarize_correction import JSON_FILE_NAME, correction_uuid

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
    (record_path.parent / JSON_FILE_NAME).write_text(json.dumps({"turns": turns}), encoding="utf-8")
    write_correction_labels(
        record_path, {first: FIXTURE["rejoin_before"], second: FIXTURE["rejoin_after"]}
    )

    assert describe_correction_speakers(record_path) == {
        first: f"Speaker A, shown as {FIXTURE['rejoin_before']}, unverified",
        second: f"Speaker B, shown as {FIXTURE['rejoin_after']}, unverified",
    }
