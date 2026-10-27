import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from graphiti_core.nodes import EpisodeType

from fakeyou.foundation.speakers import TWIN_SPEAKER, UNIDENTIFIED_PARTICIPANT
from fakeyou.memory.ingestion import episode_name, extraction_instructions, meeting_windows
from fakeyou.memory.memory import meeting_saga
from fakeyou.persistence.record import (
    RecordEntry,
    TranscriptEntry,
    TwinSpeechEntry,
)
from tests._ingestion_support import RECORD_ORDER, START, run, said, shown
from tests._memory_fakes import make_graphiti


def twin(seconds: float, text: str) -> TwinSpeechEntry:
    return TwinSpeechEntry(at=START + timedelta(seconds=seconds), seq=next(RECORD_ORDER), text=text)


def test_windows_mix_speakers_in_record_order() -> None:
    entries: list[RecordEntry] = [
        twin(0, "How about moving the demo to Thursday?"),
        said(2, "Okay,\n agreed."),
        said(3, "The budget is fine.", participant_id="Speaker A"),
    ]

    [window] = meeting_windows(entries)

    assert [(utterance.speaker, utterance.text) for utterance in window] == [
        (TWIN_SPEAKER, "How about moving the demo to Thursday?"),
        (UNIDENTIFIED_PARTICIPANT, "Okay, agreed."),
        ("Speaker A", "The budget is fine."),
    ]


def test_windows_split_by_utterance_count_and_age() -> None:
    by_count = meeting_windows([said(index, f"Sentence {index}.") for index in range(45)])
    by_age = meeting_windows(
        [
            said(0, "Start."),
            said(59, "Still going."),
            said(
                61,
                "New window with a much longer statement about the Thursday launch plan "
                "and the rollout checklist items for review this week.",
            ),
        ]
    )

    assert [len(window) for window in by_count] == [20, 20, 5]
    assert [len(window) for window in by_age] == [2, 1]


def test_windows_are_deterministic() -> None:
    entries = [said(index * 7, f"Sentence {index}.") for index in range(30)]

    assert meeting_windows(entries) == meeting_windows(list(entries))


def test_extraction_instructions_require_attribution_and_vocabulary() -> None:
    instructions = extraction_instructions("Rani Twin", ("Orchid", "Budi"))

    for phrase in (f"'{TWIN_SPEAKER}'", "'Rani Twin'", f"'{UNIDENTIFIED_PARTICIPANT}'"):
        assert phrase in instructions
    assert "never the owner" in instructions
    assert "speaker as the subject" in instructions
    assert "someone asking" in instructions
    assert "Orchid, Budi" in instructions
    assert "sounds similar" in instructions
    assert "language the speakers used" in instructions


def test_unidentified_speaker_label_leaves_owner_identity_open() -> None:
    assert UNIDENTIFIED_PARTICIPANT == "Speaker with unverified identity"
    instructions = extraction_instructions("Rani Twin", ())

    assert "may be the owner or any named person" in instructions
    assert "never merge it" in instructions


def test_extraction_instructions_without_vocabulary_do_not_list_terms() -> None:
    assert "Known names" not in extraction_instructions("Rani Twin", ())


async def test_ingest_writes_one_message_episode_per_window_in_the_meeting_saga() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])
    entries = [
        twin(0, "Move the demo to Thursday?"),
        said(1, "Okay, agreed."),
        said(
            70,
            "Budi is the PIC for the Thursday launch and owns the rollout checklist "
            "end to end without further review from the client team.",
        ),
    ]

    result, rows = await run(memory, entries)

    assert [episode["name"] for episode in memory.added] == [
        "meeting-1:w00000",
        "meeting-1:w00001",
    ]
    first = memory.added[0]
    assert first["episode_body"] == (
        f"{TWIN_SPEAKER}: Move the demo to Thursday?\n{UNIDENTIFIED_PARTICIPANT}: Okay, agreed."
    )
    assert first["source"] == EpisodeType.message
    assert first["source_description"] == "Google Meet meeting-1 transcript"
    assert first["reference_time"] == START
    assert first["group_id"] == "twin_group"
    assert first["saga"] == meeting_saga("meeting-1")
    assert "Orchid, Budi" in first["custom_extraction_instructions"]
    assert [episode["saga_previous_episode_uuid"] for episode in memory.added] == [
        None,
        "uuid-meeting-1:w00000",
    ]
    assert (result.windows, result.resumed_from, result.ingested, result.completed) == (
        2,
        0,
        2,
        True,
    )
    assert [
        (row["event"], row["facts"]) for row in rows if row["event"].startswith("memory_episode")
    ] == [("memory_episode_ingested", 1), ("memory_episode_ingested", 1)]
    [finished] = [row for row in rows if row["event"] == "memory_ingest_finished"]
    assert (finished["windows"], finished["ingested"], finished["completed"]) == (2, 2, True)


async def test_ingest_uses_spoken_time_as_the_window_source_time() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])
    spoken = datetime(2026, 9, 30, 8, 58, 30, tzinfo=UTC)
    entries = [
        TranscriptEntry(
            at=START,
            seq=next(RECORD_ORDER),
            id="late-record",
            spoken_at=spoken.isoformat(),
            source="meeting_audio",
            text="The launch stays on Friday.",
            participant_id="Speaker A",
            speaker_evidence_id=None,
        )
    ]

    result, _rows = await run(memory, entries)

    assert result.completed
    [episode] = memory.added
    assert episode["reference_time"] == spoken


async def test_ingest_falls_back_to_record_time_for_invalid_spoken_time() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])
    entries = [
        TranscriptEntry(
            at=START,
            seq=next(RECORD_ORDER),
            id="bad-clock",
            spoken_at="not-a-timestamp",
            source="meeting_audio",
            text="The launch stays on Friday.",
            participant_id="Speaker A",
            speaker_evidence_id=None,
        ),
        TranscriptEntry(
            at=START,
            seq=next(RECORD_ORDER),
            id="naive-clock",
            spoken_at="2026-09-30T09:00:00",
            source="meeting_audio",
            text="The venue stays Hall A.",
            participant_id="Speaker A",
            speaker_evidence_id=None,
        ),
    ]

    result, _rows = await run(memory, entries)

    assert result.completed
    [episode] = memory.added
    assert episode["reference_time"] == START


async def test_ingest_resumes_after_the_last_saga_episode_and_is_idempotent() -> None:
    entries = [
        said(
            index * 61,
            f"Sentence {index} covers the Thursday launch plan with the rollout checklist "
            "and the client demo schedule for all regions this week.",
        )
        for index in range(3)
    ]
    memory = make_graphiti(
        episode_prefix="uuid-",
        edges=[SimpleNamespace(fact="fact")],
        existing=[SimpleNamespace(uuid="old-0", name="meeting-1:w00000")],
    )

    result, _rows = await run(memory, entries)
    again, _rows = await run(memory, entries)

    assert [episode["name"] for episode in memory.added] == [
        "meeting-1:w00001",
        "meeting-1:w00002",
    ]
    assert memory.added[0]["saga_previous_episode_uuid"] == "old-0"
    assert (result.resumed_from, result.ingested) == (1, 2)
    assert (again.resumed_from, again.ingested, again.completed) == (3, 0, True)
    assert memory.retrieved[0] == {
        "last_n": 1,
        "group_ids": ["twin_group"],
        "saga": meeting_saga("meeting-1"),
    }


async def test_ingest_stops_at_a_failed_window_so_a_rerun_continues_there() -> None:
    entries = [
        said(
            index * 61,
            f"Sentence {index} covers the Thursday launch plan with the rollout checklist "
            "and the client demo schedule for all regions this week.",
        )
        for index in range(3)
    ]
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])
    memory.errors[1] = RuntimeError("secret graph detail")

    result, rows = await run(memory, entries)
    resumed, _rows = await run(memory, entries)

    assert (result.ingested, result.completed) == (1, False)
    assert [(row["source"], row["kind"]) for row in rows if row["event"] == "fault_recorded"] == [
        ("memory_ingest", "unknown")
    ]
    assert "secret graph detail" not in json.dumps(rows)
    assert (resumed.resumed_from, resumed.ingested, resumed.completed) == (1, 2, True)
    assert [episode["name"] for episode in memory.added] == [
        "meeting-1:w00000",
        "meeting-1:w00001",
        "meeting-1:w00002",
    ]


async def test_ingest_times_out_a_hung_window() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])
    memory.gate = asyncio.Event()

    result, rows = await run(memory, [said(0, "Stuck.")], episode_timeout_seconds=0.01)

    assert (result.ingested, result.completed) == (0, False)
    assert [row["kind"] for row in rows if row["event"] == "fault_recorded"] == ["timeout"]


async def test_ingest_stops_between_windows_when_requested() -> None:
    stop = asyncio.Event()
    stop.set()
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])

    result, _rows = await run(memory, [said(0, "One.")], stop=stop)

    assert (result.ingested, result.completed) == (0, False)
    assert memory.added == []


async def test_ingest_rejects_an_invalid_episode_name_in_the_saga() -> None:
    memory = make_graphiti(
        episode_prefix="uuid-",
        edges=[SimpleNamespace(fact="fact")],
        existing=[SimpleNamespace(uuid="invalid-episode", name="not-a-window")],
    )

    with pytest.raises(ValueError, match="invalid episode name"):
        await run(memory, [said(0, "Hello.")])


async def test_ingest_of_an_empty_record_is_complete() -> None:
    result, _rows = await run(
        make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")]), []
    )

    assert (result.windows, result.ingested, result.completed) == (0, 0, True)


def test_episode_names_sort_by_window_index() -> None:
    assert episode_name("meeting-1", 7) == "meeting-1:w00007"


async def test_ingest_result_carries_episode_uuids_per_window() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])
    entries = [
        twin(0, "Move the demo to Thursday?"),
        said(1, "Okay, agreed."),
        said(
            70,
            "Budi is the PIC for the Thursday launch and owns the rollout checklist "
            "end to end without further review from the client team.",
        ),
    ]

    result, _rows = await run(memory, entries)

    assert result.episode_uuids == {
        0: "uuid-meeting-1:w00000",
        1: "uuid-meeting-1:w00001",
    }


def long_line(index: int) -> TranscriptEntry:
    return said(
        float(index * 61),
        f"Line {index} covers the Thursday launch plan with the rollout checklist and the "
        "client demo schedule for all regions this week.",
    )


async def test_resume_over_a_long_saga_issues_one_retrieval() -> None:
    existing = [
        SimpleNamespace(uuid=f"old-{index}", name=f"meeting-1:w{index:05d}") for index in range(150)
    ]
    existing.append(SimpleNamespace(uuid="old-screen", name="meeting-1:s00000"))
    memory = make_graphiti(episode_prefix="uuid-", existing=existing)
    entries: list[RecordEntry] = [long_line(index) for index in range(160)]
    entries.append(shown("screen-1", "Launch plan", text="Slide one shows the launch plan."))

    result, _lines = await run(memory, entries)

    assert result.resumed_from == 150
    saga_retrievals = [call for call in memory.retrieved if call["last_n"] > 1]
    assert len(saga_retrievals) == 1
    assert saga_retrievals[0]["last_n"] == 2 * 160 + 1
    assert [episode["name"] for episode in memory.added] == [
        f"meeting-1:w{index:05d}" for index in range(150, 160)
    ]
    assert memory.added[0]["saga_previous_episode_uuid"] == "old-screen"


async def test_resume_from_a_transcript_head_skips_the_saga_retrieval() -> None:
    existing = [SimpleNamespace(uuid="old-0", name="meeting-1:w00000")]
    memory = make_graphiti(episode_prefix="uuid-", existing=existing)

    result, _lines = await run(memory, [long_line(index) for index in range(3)])

    assert result.resumed_from == 1
    assert [call["last_n"] for call in memory.retrieved] == [1]
