import json
from typing import Any

from tests.test_commit_boundary import after_preliminary
from tests.test_publication_activation import Meeting, ask, show_budget


async def probe() -> dict[str, Any]:
    meeting = Meeting()
    after_preliminary(meeting, show_budget)
    await ask(meeting, "When is beta?", needs=("screen",))
    current = meeting.bindings[0]
    events = [json.loads(row) for row in meeting.journal.lines]
    related = [row for row in events if row.get("publication_id") == current.publication_id]
    attempts = [
        i for i, row in enumerate(related) if row["event"] == "runtime_synthesis_attempt_requested"
    ]
    commits = [i for i, row in enumerate(related) if row["event"] == "publication_commit_decided"]
    return {
        "provider_requests": 0,
        "publication_id": current.publication_id,
        "current_receipt": meeting.settled[0].outcome,
        "current_local_audio_written": meeting.settled[0].local_audio_written,
        "synthesis_before_final_commit": bool(
            attempts and commits and min(attempts) < min(commits)
        ),
        "events": [
            {
                key: row[key]
                for key in ("event", "publication_id", "validity", "reason", "attempt_number")
                if key in row
            }
            for row in related
        ],
        "constraint_cycle": [
            "NC-76 requires final committed-valid before current synthesis",
            "Dynamic first PCM requires synthesis",
            "I7 requires final evaluation and first-PCM handoff together without suspension",
        ],
        "delayed_post_commit_synthesis_example": {
            "evaluated_at": 100.0,
            "first_pcm_ready_at": 100.3,
            "validation_allowance_seconds": 0.25,
            "can_write_without_recommit": False,
            "recommit_would_leave_original_synthesis_before_final_commit": True,
        },
    }
