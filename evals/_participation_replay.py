import asyncio
import json
from pathlib import Path
from typing import Any

from fakeyou.conversation.conversation import ConversationLoop, LoopInteraction
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.diagnostics import Diagnostics
from tests._conversation_fakes import FakeResponder
from tests._persistence_fakes import RecordingJournal

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_record_turns(path: Path) -> list[TranscriptEvent]:
    turns: list[TranscriptEvent] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("kind") != "transcript":
            continue
        turns.append(
            TranscriptEvent(
                id=row["id"],
                timestamp=row.get("spoken_at", row.get("at", "")),
                source="meeting_audio",
                text=row["text"],
                finalized=True,
                participant_id=row.get("participant", "unknown"),
                speaker_evidence_id=None,
            )
        )
    return turns


def load_labels(session: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "participation-labels.json").read_text())
    session_data: dict[str, Any] = data[session]
    return session_data


def summarize_policy(rows: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {"rule": 0, "jev": 0, "stale_pregen": 0}
    for row in rows:
        if row.get("event") != "policy_decided":
            continue
        source = row.get("decision_source", "rule")
        if source in summary:
            summary[source] += 1
        if row.get("reason") == "jev_stale_silent":
            summary["stale_pregen"] += 1
    return summary


def replay_record(path: Path, twin_names: tuple[str, ...]) -> list[dict[str, Any]]:
    async def _run() -> list[dict[str, Any]]:
        journal = RecordingJournal(meeting_id="replay")
        loop = ConversationLoop(
            FakeResponder(),
            interaction=LoopInteraction(twin_names=twin_names),
            diagnostics=Diagnostics(journal=journal),
        )
        for turn in load_record_turns(path):
            await loop.handle_transcript(turn)
        await loop.wait_idle()
        await loop.close()
        return [
            {
                "transcript_id": row["transcript_id"],
                "action": row["action"],
                "reason": row["reason"],
            }
            for row in journal.events("policy_decided")
        ]

    return asyncio.run(_run())


def score_replay(decisions: list[dict[str, Any]], turns: list[dict[str, Any]]) -> dict[str, Any]:
    predicted = {
        decision["transcript_id"]: (
            "speak" if decision["action"] in ("reply", "clarify") else "silent"
        )
        for decision in decisions
    }
    misses = [turn["id"] for turn in turns if predicted.get(turn["id"]) != turn["label"]]
    total = len(turns)
    return {
        "turns": total,
        "agreement": ((total - len(misses)) / total) if total else 0.0,
        "misses": misses,
    }
