import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage

from fakeyou.conversation.contribution import memory_refs_from_evidence, source_registry
from fakeyou.conversation.conversation import MeetingAgent, MeetingResponder
from fakeyou.conversation.grounding import GroundingVerdict
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.foundation.transcript import AttentionContext, TranscriptEvent
from tests._persistence_fakes import RecordingJournal

PLAN = json.dumps(
    {
        "understood": "screen changed",
        "language": "en",
        "intent": "answer",
        "text": "The beta actually launches on 14 October.",
        "contribution_kind": "correction",
        "contribution_source": "profile",
        "source_ref": "fact-0",
    }
)


class _Agent:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def ainvoke(self, payload: Any, config: Any = None, **kwargs: Any) -> dict[str, Any]:
        del payload, config, kwargs
        return {"messages": [AIMessage(content=self._reply)]}


class _Verifier:
    def __init__(self, verdict: GroundingVerdict | Exception) -> None:
        self._verdict = verdict
        self.calls: list[dict[str, Any]] = []

    async def verify(
        self,
        *,
        text: str,
        source_text: str,
        trigger_text: str,
        kind: str | None,
        recent_lines: Sequence[str] = (),
    ) -> GroundingVerdict:
        self.calls.append(
            {
                "text": text,
                "source_text": source_text,
                "trigger_text": trigger_text,
                "kind": kind,
                "recent_lines": recent_lines,
            }
        )
        if isinstance(self._verdict, Exception):
            raise self._verdict
        return self._verdict


def _attention() -> TranscriptEvent:
    return TranscriptEvent(
        id="attention-1",
        timestamp=datetime.now(UTC).isoformat(),
        source="meeting_attention",
        text="The screen changed.",
        finalized=True,
        participant_id="unknown",
        speaker_evidence_id=None,
        attention=AttentionContext(
            kind="screen",
            trigger_text="Launch Timeline: beta slips to 20 October",
            trigger_event_id="screen#1",
        ),
    )


def _responder(verifier: _Verifier | None, journal: RecordingJournal) -> MeetingResponder:
    return MeetingResponder(
        cast(MeetingAgent, _Agent(PLAN)),
        NoHistory(),
        journal=journal,
        language="en",
        source_registry=source_registry(
            profile_facts=["The Orchid beta launch is planned for 14 October 2026"]
        ),
        verifier=verifier,
    )


async def test_a_grounded_contribution_is_spoken() -> None:
    journal = RecordingJournal()
    verifier = _Verifier(
        GroundingVerdict(grounded=True, adds_information=True, on_topic=True, confidence=0.9)
    )
    chunk = await _responder(verifier, journal).respond([_attention()], lambda: None)

    assert chunk.text == "The beta actually launches on 14 October."
    assert verifier.calls[0]["source_text"].endswith("14 October 2026")
    assert journal.events("contribution_verified")
    assert journal.events("reply_plan_observed")[0]["gate"] == "pass"


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        (
            GroundingVerdict(grounded=False, adds_information=True, on_topic=True, confidence=0.9),
            "not_grounded",
        ),
        (
            GroundingVerdict(grounded=True, adds_information=False, on_topic=True, confidence=0.9),
            "adds_nothing",
        ),
        (
            GroundingVerdict(grounded=True, adds_information=True, on_topic=True, confidence=0.4),
            "low_confidence",
        ),
        (RuntimeError("down"), "verifier_failed"),
    ],
)
async def test_a_failed_verification_stays_silent(
    verdict: GroundingVerdict | Exception, reason: str
) -> None:
    journal = RecordingJournal()
    chunk = await _responder(_Verifier(verdict), journal).respond([_attention()], lambda: None)

    assert chunk.text == ""
    assert journal.events("contribution_dropped")[0]["reason"] == reason


async def test_without_a_verifier_the_lane_fails_closed() -> None:
    journal = RecordingJournal()
    chunk = await _responder(None, journal).respond([_attention()], lambda: None)

    assert chunk.text == ""
    assert journal.events("contribution_dropped")[0]["reason"] == "verifier_unavailable"


async def test_the_streaming_path_verifies_before_speaking() -> None:
    journal = RecordingJournal()
    verifier = _Verifier(
        GroundingVerdict(grounded=True, adds_information=True, on_topic=True, confidence=0.9)
    )
    responder = _responder(verifier, journal)

    chunks = [
        chunk
        async for chunk in responder.stream_sentences([_attention()], lambda: None, request_id="r1")
    ]

    assert [chunk.text for chunk in chunks] == ["The beta actually launches on 14 October."]


def test_memory_refs_come_from_rendered_evidence() -> None:
    payload = json.dumps(
        {"schema": "x", "facts": [{"fact_ids": ["edge-1", "edge-2"], "text": "Beta is 14 Oct"}]}
    )

    assert memory_refs_from_evidence(payload) == {
        "edge-1": "Beta is 14 Oct",
        "edge-2": "Beta is 14 Oct",
    }
    assert memory_refs_from_evidence("No relevant meeting memory found.") == {}
