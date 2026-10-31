import asyncio
import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.session import SessionConfig
from fakeyou.observability.journal import journal_path
from fakeyou.persistence.record import meeting_record_path
from fakeyou.replay.fixture import load_fixture
from fakeyou.replay.harness import RecordedBehavior, ReplayResult, recorded_behavior, replay_session
from fakeyou.replay.virtual_time import VirtualTimeLoop

SESSION_FILE_NAME = "session.json"


def replay_meeting(
    meeting_id: SessionId,
    *,
    settings: Settings,
    journal_dir: Path,
    record_dir: Path,
    out: Path | None = None,
) -> ReplayResult:
    record = meeting_record_path(record_dir, meeting_id)
    journal = journal_path(journal_dir, meeting_id)
    if not (journal.exists() and record.exists()):
        raise FakeYouError(ErrorCode.REPLAY_SESSION_MISSING, meeting_id=meeting_id)
    fixture = load_fixture(journal, record)
    session = record.parent / SESSION_FILE_NAME
    behavior = recorded_behavior(session) if session.exists() else RecordedBehavior()
    config = SessionConfig(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        meeting_id=meeting_id,
        link="https://meet.google.com/replay-only",
        twin_name=behavior.twin_names[0] if behavior.twin_names else settings.identity.twin_name,
        participant_policy_enabled=behavior.participant_policy_enabled,
        per_stream_enabled=settings.conversation.per_stream_enabled,
    )
    with tempfile.TemporaryDirectory() as scratch:
        replay_record = Path(scratch) / "record.jsonl"
        with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
            result = runner.run(
                replay_session(
                    fixture, settings, config, behavior=behavior, record_path=replay_record
                )
            )
    if out is not None:
        out.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in result.rows),
            encoding="utf-8",
        )
    return result


def replay_report(meeting_id: str, result: ReplayResult) -> dict[str, object]:
    comparison = result.comparison
    return {
        "meeting_id": meeting_id,
        "passed": comparison.passed,
        "comparable": comparison.comparable,
        "gated": [asdict(check) for check in comparison.gated()],
        "reported": [asdict(check) for check in comparison.reported()],
    }
