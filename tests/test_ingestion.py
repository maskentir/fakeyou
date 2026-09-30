import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from graphiti_core.nodes import EpisodeType

from fakeyou.faults import FaultReporter
from fakeyou.ingestion import (
    TWIN_SPEAKER,
    UNIDENTIFIED_PARTICIPANT,
    episode_name,
    extraction_instructions,
    ingest_meeting,
    meeting_windows,
)
from fakeyou.journal import ProviderJournal
from fakeyou.memory import meeting_saga
from fakeyou.record import ToolResultEntry, TranscriptEntry, TwinSpeechEntry

START = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)


def said(seconds: float, text: str, participant: str = "unknown") -> TranscriptEntry:
    return TranscriptEntry(
        at=START + timedelta(seconds=seconds),
        id=f"t{seconds}",
        spoken_at=(START + timedelta(seconds=seconds)).isoformat(),
        participant=participant,
        source="meeting_audio",
        text=text,
    )


def twin(seconds: float, text: str) -> TwinSpeechEntry:
    return TwinSpeechEntry(at=START + timedelta(seconds=seconds), text=text)


class FakeGraphiti:
    def __init__(self, existing: list[SimpleNamespace] | None = None) -> None:
        self.saga_episodes = list(existing or [])
        self.added: list[dict] = []
        self.errors: dict[int, BaseException] = {}
        self.gate: asyncio.Event | None = None
        self.retrieved: list[dict] = []
        self.driver = SimpleNamespace(name="driver")

    async def retrieve_episodes(self, reference_time, **kwargs):
        self.retrieved.append(kwargs)
        return self.saga_episodes[-kwargs["last_n"] :]

    async def add_episode(self, **kwargs):
        if self.gate is not None:
            await self.gate.wait()
        attempt = len(self.added)
        if attempt in self.errors:
            raise self.errors.pop(attempt)
        self.added.append(kwargs)
        episode = SimpleNamespace(uuid=f"uuid-{kwargs['name']}", name=kwargs["name"])
        self.saga_episodes.append(episode)
        return SimpleNamespace(episode=episode, edges=[SimpleNamespace(fact="fact")])


async def run(memory: FakeGraphiti, entries: list, **overrides):
    lines: list[str] = []
    values = {
        "group_id": "twin_group",
        "meeting_id": "meeting-1",
        "twin_name": "Rani Twin",
        "vocabulary": ("Orchid", "Budi"),
        "journal": ProviderJournal("meeting-1", emit=lines.append),
    }
    values["faults"] = FaultReporter(journal=values["journal"])
    values.update(overrides)
    result = await ingest_meeting(memory, entries, **values)
    return result, [json.loads(line) for line in lines]


def test_windows_mix_speakers_in_record_order_and_skip_tool_results() -> None:
    entries = [
        twin(0, "How about moving the demo to Thursday?"),
        said(2, "Okay,\n agreed."),
        ToolResultEntry(at=START, tool="recall_meeting_context", output="x", duration_ms=1),
        said(3, "The budget is fine.", participant="Speaker A"),
    ]

    [window] = meeting_windows(entries)

    assert [(utterance.speaker, utterance.text) for utterance in window] == [
        (TWIN_SPEAKER, "How about moving the demo to Thursday?"),
        (UNIDENTIFIED_PARTICIPANT, "Okay, agreed."),
        ("Speaker A", "The budget is fine."),
    ]


def test_windows_split_by_utterance_count_and_age() -> None:
    by_count = meeting_windows([said(index, f"Sentence {index}.") for index in range(45)])
    by_age = meeting_windows([said(0, "Start."), said(59, "Still going."), said(61, "New window.")])

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


def test_extraction_instructions_without_vocabulary_do_not_list_terms() -> None:
    assert "Known names" not in extraction_instructions("Rani Twin", ())


async def test_ingest_writes_one_message_episode_per_window_in_the_meeting_saga() -> None:
    memory = FakeGraphiti()
    entries = [
        twin(0, "Move the demo to Thursday?"),
        said(1, "Okay, agreed."),
        said(70, "Budi is the PIC."),
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
    assert [row["outcome"] for row in rows if row["event"] == "memory_episode"] == [
        "extracted",
        "extracted",
    ]
    [finished] = [row for row in rows if row["event"] == "memory_ingest_finished"]
    assert (finished["windows"], finished["ingested"], finished["completed"]) == (2, 2, True)


async def test_ingest_resumes_after_the_last_saga_episode_and_is_idempotent() -> None:
    entries = [said(index * 61, f"Sentence {index}.") for index in range(3)]
    memory = FakeGraphiti(existing=[SimpleNamespace(uuid="old-0", name="meeting-1:w00000")])

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
    entries = [said(index * 61, f"Sentence {index}.") for index in range(3)]
    memory = FakeGraphiti()
    memory.errors[1] = RuntimeError("secret graph detail")

    result, rows = await run(memory, entries)
    resumed, _rows = await run(memory, entries)

    assert (result.ingested, result.completed) == (1, False)
    assert [(row["source"], row["kind"]) for row in rows if row["event"] == "fault"] == [
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
    memory = FakeGraphiti()
    memory.gate = asyncio.Event()

    result, rows = await run(memory, [said(0, "Stuck.")], episode_timeout_seconds=0.01)

    assert (result.ingested, result.completed) == (0, False)
    assert [row["kind"] for row in rows if row["event"] == "fault"] == ["timeout"]


async def test_ingest_stops_between_windows_when_requested() -> None:
    stop = asyncio.Event()
    stop.set()
    memory = FakeGraphiti()

    result, _rows = await run(memory, [said(0, "One.")], stop=stop)

    assert (result.ingested, result.completed) == (0, False)
    assert memory.added == []


async def test_ingest_refuses_a_saga_written_by_an_older_ingestion_format() -> None:
    memory = FakeGraphiti(existing=[SimpleNamespace(uuid="legacy", name="meeting-1:00003")])

    with pytest.raises(ValueError, match="memory-forget"):
        await run(memory, [said(0, "Hello.")])


async def test_ingest_of_an_empty_record_is_complete() -> None:
    result, _rows = await run(FakeGraphiti(), [])

    assert (result.windows, result.ingested, result.completed) == (0, 0, True)


def test_episode_names_sort_by_window_index() -> None:
    assert episode_name("meeting-1", 7) == "meeting-1:w00007"
