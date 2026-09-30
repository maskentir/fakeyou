import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from graphiti_core.nodes import EpisodeType

from fakeyou.conversation import TranscriptEvent
from fakeyou.faults import FaultReporter
from fakeyou.ingestion import TWIN_SPEAKER, UNIDENTIFIED_PARTICIPANT, MeetingIngestion
from fakeyou.journal import ProviderJournal
from fakeyou.memory import meeting_saga

TWIN_NAME = "Rani Twin"
SPOKEN_AT = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)


class FakeGraphiti:
    def __init__(self, *, edges_per_episode: int = 1) -> None:
        self.episodes: list[dict] = []
        self.edges_per_episode = edges_per_episode
        self.errors: list[BaseException] = []
        self.gate: asyncio.Event | None = None
        self.added = asyncio.Event()

    async def add_episode(self, **kwargs):
        if self.gate is not None:
            await self.gate.wait()
        if self.errors:
            raise self.errors.pop(0)
        self.episodes.append(kwargs)
        self.added.set()
        return SimpleNamespace(
            episode=SimpleNamespace(uuid=f"episode-{len(self.episodes)}"),
            edges=[SimpleNamespace(fact="fact")] * self.edges_per_episode,
        )


def transcript(index: int, text: str = "Demo Orchid geser ke Kamis.", **overrides):
    fields = {
        "id": f"event-{index}",
        "timestamp": (SPOKEN_AT + timedelta(seconds=index)).isoformat(),
        "source": "meeting_audio",
        "text": text,
        "finalized": True,
    }
    fields.update(overrides)
    return TranscriptEvent(**fields)


def make_ingestion(memory: FakeGraphiti, **overrides) -> tuple[MeetingIngestion, list[str]]:
    lines: list[str] = []
    options = {
        "group_id": "twin_group",
        "meeting_id": "meeting-1",
        "twin_name": TWIN_NAME,
        "faults": FaultReporter(meeting_id="meeting-1", log=lines.append),
        "journal": ProviderJournal("meeting-1", emit=lines.append),
        "now": lambda: SPOKEN_AT,
    }
    options.update(overrides)
    return MeetingIngestion(memory, **options), lines


def events(lines: list[str], name: str) -> list[dict]:
    rows = [json.loads(line) for line in lines]
    return [row for row in rows if row["event"] == name]


async def test_participant_transcripts_become_one_message_episode_in_meeting_saga() -> None:
    memory = FakeGraphiti()
    ingestion, _lines = make_ingestion(memory)

    ingestion.add_transcript(transcript(0, "Demo Orchid geser ke Kamis."))
    ingestion.add_transcript(transcript(1, "Oke, setuju."))
    await ingestion.close()

    [episode] = memory.episodes
    assert episode["episode_body"] == (
        f"{UNIDENTIFIED_PARTICIPANT}: Demo Orchid geser ke Kamis.\n"
        f"{UNIDENTIFIED_PARTICIPANT}: Oke, setuju."
    )
    assert episode["source"] == EpisodeType.message
    assert episode["group_id"] == "twin_group"
    assert episode["saga"] == meeting_saga("meeting-1")
    assert episode["reference_time"] == SPOKEN_AT
    assert episode["source_description"] == "Google Meet meeting-1 participant transcript"
    instructions = episode["custom_extraction_instructions"]
    for label in (TWIN_SPEAKER, TWIN_NAME, UNIDENTIFIED_PARTICIPANT):
        assert f"'{label}'" in instructions
    assert "never the owner" in instructions


async def test_known_participant_keeps_their_name() -> None:
    memory = FakeGraphiti()
    ingestion, _lines = make_ingestion(memory)

    ingestion.add_transcript(transcript(0, "Budget aman.", participant="Budi"))
    await ingestion.close()

    assert memory.episodes[0]["episode_body"] == "Budi: Budget aman."


async def test_only_new_final_nonblank_transcripts_are_ingested() -> None:
    memory = FakeGraphiti()
    ingestion, _lines = make_ingestion(memory)

    ingestion.add_transcript(transcript(0, "Pertama."))
    ingestion.add_transcript(transcript(0, "Pertama."))
    ingestion.add_transcript(transcript(1, "sementara", finalized=False))
    ingestion.add_transcript(transcript(2, "   "))
    await ingestion.close()

    assert memory.episodes[0]["episode_body"] == f"{UNIDENTIFIED_PARTICIPANT}: Pertama."


async def test_speaker_kind_change_starts_a_new_chained_episode() -> None:
    memory = FakeGraphiti()
    ingestion, _lines = make_ingestion(memory)

    ingestion.add_twin_speech("Gimana kalau demo digeser ke Kamis?")
    ingestion.add_transcript(transcript(1, "Oke, setuju."))
    ingestion.add_twin_speech("Siap, aku catat.")
    await ingestion.close()

    assert [episode["episode_body"] for episode in memory.episodes] == [
        f"{TWIN_SPEAKER}: Gimana kalau demo digeser ke Kamis?",
        f"{UNIDENTIFIED_PARTICIPANT}: Oke, setuju.",
        f"{TWIN_SPEAKER}: Siap, aku catat.",
    ]
    assert [episode["source_description"] for episode in memory.episodes] == [
        "Google Meet meeting-1 twin speech",
        "Google Meet meeting-1 participant transcript",
        "Google Meet meeting-1 twin speech",
    ]
    assert [episode["saga_previous_episode_uuid"] for episode in memory.episodes] == [
        None,
        "episode-1",
        "episode-2",
    ]


async def test_utterance_whitespace_is_normalized_to_one_line() -> None:
    memory = FakeGraphiti()
    ingestion, _lines = make_ingestion(memory)

    ingestion.add_transcript(transcript(0, "  Demo\nOrchid   Kamis  "))
    ingestion.add_twin_speech("Oke.\n\nSiap.")
    await ingestion.close()

    assert [episode["episode_body"] for episode in memory.episodes] == [
        f"{UNIDENTIFIED_PARTICIPANT}: Demo Orchid Kamis",
        f"{TWIN_SPEAKER}: Oke. Siap.",
    ]


async def test_batch_flushes_at_utterance_limit() -> None:
    memory = FakeGraphiti()
    ingestion, _lines = make_ingestion(memory, max_utterances=2)

    for index in range(5):
        ingestion.add_transcript(transcript(index, f"Kalimat {index}."))
    await ingestion.close()

    assert [episode["episode_body"].count("\n") + 1 for episode in memory.episodes] == [2, 2, 1]


async def test_quiet_window_flushes_without_close() -> None:
    memory = FakeGraphiti()
    ingestion, _lines = make_ingestion(memory, window_seconds=0.01)

    ingestion.add_transcript(transcript(0))
    async with asyncio.timeout(2):
        await memory.added.wait()

    assert len(memory.episodes) == 1
    await ingestion.close()


async def test_window_age_flushes_a_continuous_stream() -> None:
    memory = FakeGraphiti()
    ticks = iter(range(1000))
    ingestion, _lines = make_ingestion(
        memory, window_seconds=2, max_utterances=100, clock=lambda: next(ticks)
    )

    for index in range(5):
        ingestion.add_transcript(transcript(index, f"Kalimat {index}."))
    await ingestion.close()

    assert len(memory.episodes) >= 2
    assert sum(episode["episode_body"].count("\n") + 1 for episode in memory.episodes) == 5


async def test_failed_episode_is_reported_and_ingestion_continues() -> None:
    memory = FakeGraphiti()
    memory.errors.append(RuntimeError("secret neo4j detail"))
    ingestion, lines = make_ingestion(memory, max_utterances=1)

    ingestion.add_transcript(transcript(0, "Gagal."))
    ingestion.add_transcript(transcript(1, "Berhasil."))
    await ingestion.close()

    assert [episode["episode_body"] for episode in memory.episodes] == [
        f"{UNIDENTIFIED_PARTICIPANT}: Berhasil."
    ]
    faults = events(lines, "fault")
    assert [(fault["source"], fault["kind"]) for fault in faults] == [("memory_ingest", "unknown")]
    assert "secret neo4j detail" not in "\n".join(lines)
    assert [row["outcome"] for row in events(lines, "memory_episode")] == ["failed", "extracted"]


async def test_hung_episode_times_out_and_ingestion_continues() -> None:
    memory = FakeGraphiti()
    fault_lines: list[str] = []
    reported = asyncio.Event()

    def log_fault(line: str) -> None:
        fault_lines.append(line)
        reported.set()

    ingestion, _lines = make_ingestion(
        memory,
        max_utterances=1,
        episode_timeout_seconds=0.01,
        faults=FaultReporter(meeting_id="meeting-1", log=log_fault),
    )
    memory.gate = asyncio.Event()

    ingestion.add_transcript(transcript(0, "Macet."))
    async with asyncio.timeout(2):
        await reported.wait()
    memory.gate.set()
    ingestion.add_transcript(transcript(1, "Lanjut."))
    await ingestion.close()

    assert [episode["episode_body"] for episode in memory.episodes] == [
        f"{UNIDENTIFIED_PARTICIPANT}: Lanjut."
    ]
    assert events(fault_lines, "fault")[0]["kind"] == "timeout"


async def test_episode_without_extracted_facts_is_journaled_as_empty() -> None:
    memory = FakeGraphiti(edges_per_episode=0)
    ingestion, lines = make_ingestion(memory)

    ingestion.add_transcript(transcript(0, "Halo semua."))
    await ingestion.close()

    [row] = events(lines, "memory_episode")
    assert row["outcome"] == "empty"
    assert row["utterances"] == 1
    assert row["facts"] == 0


async def test_full_queue_drops_utterances_without_blocking() -> None:
    memory = FakeGraphiti()
    memory.gate = asyncio.Event()
    ingestion, lines = make_ingestion(memory, queue_size=2, max_utterances=1)

    for index in range(6):
        ingestion.add_transcript(transcript(index, f"Kalimat {index}."))
    memory.gate.set()
    await ingestion.close()

    assert 1 <= len(memory.episodes) < 6
    [fault] = events(lines, "fault")
    assert (fault["source"], fault["kind"]) == ("memory_ingest_backlog", "unavailable")


async def test_close_timeout_cancels_hung_ingestion_and_reports_backlog() -> None:
    memory = FakeGraphiti()
    memory.gate = asyncio.Event()
    ingestion, lines = make_ingestion(memory, max_utterances=1, drain_timeout_seconds=0.05)

    ingestion.add_transcript(transcript(0, "Macet."))
    ingestion.add_transcript(transcript(1, "Tertahan."))
    async with asyncio.timeout(2):
        await ingestion.close()

    assert memory.episodes == []
    faults = events(lines, "fault")
    assert [(fault["source"], fault["kind"]) for fault in faults] == [("memory_ingest", "timeout")]


async def test_utterances_after_close_are_ignored() -> None:
    memory = FakeGraphiti()
    ingestion, _lines = make_ingestion(memory)

    await ingestion.close()
    ingestion.add_transcript(transcript(0))
    ingestion.add_twin_speech("Halo.")
    await asyncio.sleep(0)

    assert memory.episodes == []


async def test_invalid_transcript_timestamp_falls_back_to_now() -> None:
    memory = FakeGraphiti()
    ingestion, _lines = make_ingestion(memory)

    ingestion.add_transcript(transcript(0, timestamp="not-a-time"))
    await ingestion.close()

    assert memory.episodes[0]["reference_time"] == SPOKEN_AT


async def test_close_is_idempotent_and_leaves_no_task() -> None:
    memory = FakeGraphiti()
    ingestion, _lines = make_ingestion(memory)

    ingestion.add_transcript(transcript(0))
    await ingestion.close()
    await ingestion.close()

    assert len(memory.episodes) == 1
    assert all(task.done() for task in asyncio.all_tasks() if task is not asyncio.current_task())
