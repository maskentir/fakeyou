import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from evals._shared import token_present
from fakeyou.config.settings import DEFAULTS
from fakeyou.diarization.diarize_client import DiarizedTurn
from fakeyou.diarization.diarize_correction import write_correction_bundle
from fakeyou.memory.memory import search_memory_evidence
from fakeyou.persistence.record import TranscriptEntry

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "memory-correction-v1.json"


def load_correction_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def query_matches(answer: str, present: list[str], absent: list[str]) -> bool:
    return all(token_present(token, answer) for token in present) and not any(
        token_present(token, answer) for token in absent
    )


def superseded_serve_rate(answers: list[str], stale: str) -> float:
    if not answers:
        return 0.0
    return sum(1 for answer in answers if token_present(stale, answer)) / len(answers)


def write_fixture_record(target: Path, fixture: dict[str, Any]) -> Path:
    base = datetime.fromisoformat(str(fixture["base_at"]))
    lines: list[str] = []
    counter = 0
    for window in fixture["windows"]:
        for utterance in window["utterances"]:
            moment = base + timedelta(seconds=int(utterance["offset_seconds"]))
            entry = TranscriptEntry(
                at=moment,
                id=f"u{counter:02d}",
                spoken_at=moment.isoformat(),
                source="meeting_audio",
                text=str(utterance["text"]),
                participant_id=str(utterance["speaker"]),
                speaker_evidence_id=None,
                capture_ms=float(utterance["offset_seconds"]) * 1000.0,
            )
            lines.append(entry.model_dump_json())
            counter += 1
    record_path = target / str(fixture["meeting_id"]) / "record.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    correction = fixture["correction"]
    write_correction_bundle(
        record_path.parent / "audio",
        record_path,
        [
            DiarizedTurn(
                start_ms=int(correction["start_ms"]),
                end_ms=int(correction["end_ms"]),
                speaker=str(correction["speaker"]),
            )
        ],
        clock=lambda: datetime.now(UTC),
    )
    return record_path


async def answer_queries(
    memory: Any, queries: list[dict[str, Any]], *, group_id: str
) -> dict[str, str]:
    recall = DEFAULTS.memory.recall
    answers: dict[str, str] = {}
    for item in queries:
        answers[str(item["id"])] = await search_memory_evidence(
            memory, str(item["query"]), group_id=group_id, recall=recall
        )
    return answers
