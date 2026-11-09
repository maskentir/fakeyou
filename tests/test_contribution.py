import json
from datetime import UTC, datetime

from fakeyou.conversation.contribution import RecalledFact, memory_refs_from_evidence
from fakeyou.foundation.provenance import OWNER_SPEAKER_IDENTITY


def test_memory_refs_come_from_rendered_evidence() -> None:
    payload = json.dumps(
        {
            "schema": "x",
            "facts": [
                {
                    "fact_ids": ["edge-1", "edge-2"],
                    "text": "Beta is 14 Oct",
                    "sources": [
                        {"kind": "meeting_transcript", "source_at": "2026-10-01T03:00:00+00:00"}
                    ],
                }
            ],
        }
    )
    recalled = RecalledFact("Beta is 14 Oct", (datetime(2026, 10, 1, 3, 0, tzinfo=UTC),))

    assert memory_refs_from_evidence(payload) == {"edge-1": recalled, "edge-2": recalled}
    assert memory_refs_from_evidence("No relevant meeting memory found.") == {}


def test_only_a_fact_whose_every_source_is_the_verified_owner_is_owner_spoken() -> None:
    owner = {"speaker_verified": True, "speaker_identity": OWNER_SPEAKER_IDENTITY}
    claimed = {"speaker_verified": False, "speaker_identity": OWNER_SPEAKER_IDENTITY}
    payload = json.dumps(
        {
            "facts": [
                {"fact_ids": ["edge-owner"], "text": "Weekly demos", "sources": [owner]},
                {"fact_ids": ["edge-mixed"], "text": "Ship Friday", "sources": [owner, claimed]},
                {"fact_ids": ["edge-bare"], "text": "No source", "sources": []},
            ]
        }
    )

    refs = memory_refs_from_evidence(payload)

    assert refs["edge-owner"].owner_spoken
    assert not refs["edge-mixed"].owner_spoken
    assert not refs["edge-bare"].owner_spoken
