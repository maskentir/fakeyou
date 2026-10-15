import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from fakeyou.diarization.correction_labels import write_correction_labels
from fakeyou.diarization.diarize_correction import correction_uuid
from fakeyou.foundation.speakers import UNIDENTIFIED_PARTICIPANT
from fakeyou.memory.ingestion import MeetingScope, ingest_corrections
from fakeyou.persistence.layout import CORRECTION_LABELS_FILE_NAME
from fakeyou.persistence.record import TranscriptEntry
from tests._ingestion_support import START, correction_record, run, said
from tests._memory_fakes import make_graphiti

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "correction_labels.json").read_text(encoding="utf-8")
)


@pytest.fixture
def golden_meeting_entries() -> list[TranscriptEntry]:
    spoken = datetime(2026, 9, 30, 8, 58, 30, tzinfo=UTC)
    return [
        said(0, "The budget review starts now with the full team present."),
        said(1, "Joko will present the spending summary for this quarter."),
        TranscriptEntry(
            at=START + timedelta(seconds=70),
            id="golden-spoken",
            spoken_at=spoken.isoformat(),
            source="meeting_audio",
            text="The client demo moves to Thursday for all regions.",
            participant_id="unknown",
            speaker_evidence_id=None,
        ),
        said(71, "Nadia will send the meeting notes by Friday evening."),
        said(140, "Noted."),
    ]


async def test_lone_trailing_utterance_attaches_to_previous_window() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])
    entries = [
        said(0, "The budget review starts now with the full team present."),
        said(1, "Joko will present the spending summary for this quarter."),
        said(70, "Agreed."),
    ]

    result, _rows = await run(memory, entries)

    assert (result.windows, result.ingested, result.completed) == (1, 1, True)
    [episode] = memory.added
    assert episode["episode_body"] == (
        f"{UNIDENTIFIED_PARTICIPANT}: The budget review starts now with the full team present.\n"
        f"{UNIDENTIFIED_PARTICIPANT}: Joko will present the spending summary for this quarter.\n"
        f"{UNIDENTIFIED_PARTICIPANT}: Agreed."
    )


async def test_whole_meeting_single_utterance_ingests_exactly_once() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])

    result, _rows = await run(memory, [said(0, "We ship on Friday.")])

    assert (result.windows, result.resumed_from, result.ingested, result.completed) == (
        1,
        0,
        1,
        True,
    )
    assert [episode["name"] for episode in memory.added] == ["meeting-1:w00000"]


async def test_golden_episode_body_keeps_speaker_subject_saga_order_and_source_time(
    golden_meeting_entries: list[TranscriptEntry],
) -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])

    result, _rows = await run(memory, golden_meeting_entries)

    assert result.windows == 2
    assert [episode["name"] for episode in memory.added] == [
        "meeting-1:w00000",
        "meeting-1:w00001",
    ]
    for episode in memory.added:
        for line in episode["episode_body"].split("\n"):
            head, separator, tail = line.partition(": ")
            assert separator
            assert head
            assert tail
    assert [episode["saga_previous_episode_uuid"] for episode in memory.added] == [
        None,
        "uuid-meeting-1:w00000",
    ]
    first, second = memory.added
    assert first["reference_time"] == START
    assert second["reference_time"] == datetime(2026, 9, 30, 8, 58, 30, tzinfo=UTC)
    assert second["episode_body"].split("\n")[-1] == f"{UNIDENTIFIED_PARTICIPANT}: Noted."


async def test_ingest_corrections_renders_labels_in_the_corrected_episode_body(
    tmp_path: Path,
) -> None:
    record_path = correction_record(tmp_path, speaker="A")
    uuid = correction_uuid("A", 69000)
    write_correction_labels(record_path, {uuid: FIXTURE["render_shown"]})
    memory = make_graphiti(episode_prefix="uuid-", edges=[])

    result = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert result.changed_windows == 1
    assert result.episodes == ("uuid-meeting-1:w00001:corrected",)
    assert (
        f"Speaker A, shown as {FIXTURE['render_shown']}, unverified"
        in (memory.added[-1]["episode_body"])
    )


async def test_ingest_corrections_without_labels_keeps_raw_speaker_bytes(
    tmp_path: Path,
) -> None:
    record_path = correction_record(tmp_path, speaker="A")
    memory = make_graphiti(episode_prefix="uuid-", edges=[])

    result = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert result.changed_windows == 1
    assert memory.added[-1]["episode_body"] == (
        "A: Budi is the PIC for the Thursday launch and owns the rollout checklist "
        "end to end without further review from the client team."
    )


@pytest.mark.parametrize("payload", ["{not json", "[]", '{"uuid": 123}'])
async def test_ingest_corrections_treats_invalid_labels_as_absent(
    tmp_path: Path, payload: str
) -> None:
    record_path = correction_record(tmp_path, speaker="A")
    (record_path.parent / CORRECTION_LABELS_FILE_NAME).write_text(payload, encoding="utf-8")
    memory = make_graphiti(episode_prefix="uuid-", edges=[])

    result = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert result.changed_windows == 1
    assert memory.added[-1]["episode_body"] == (
        "A: Budi is the PIC for the Thursday launch and owns the rollout checklist "
        "end to end without further review from the client team."
    )
    assert "unverified" not in memory.added[-1]["episode_body"]
