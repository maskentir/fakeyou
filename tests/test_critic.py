from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast, override

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from fakeyou.conversation.contribution import OwnerModelSource, source_registry
from fakeyou.conversation.conversation import MeetingAgent, MeetingResponder
from fakeyou.conversation.critic import CriticClaim, CriticVerdict
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.conversation.speech_book import BookSource
from fakeyou.foundation.transcript import AttentionContext, TranscriptEvent
from fakeyou.session.speech import BookEntry
from tests._conversation_support import claim, event, plan_reply
from tests._persistence_fakes import RecordingJournal

FACT_PLAN = plan_reply(
    claim("The beta actually launches on 14 October.", "om-beta"), understood="screen changed"
)
POSITION_PLAN = plan_reply(
    claim(
        "I would hold the launch until the migration is done.",
        "om-beta",
        act="opinion",
    ),
    understood="should we delay the launch?",
)
FACT_ANSWER = plan_reply(
    claim("The launch is on 14 October.", "om-beta"), understood="when is the launch?"
)
UNSURE = "I'm not sure about that, so I'd rather not guess."


def _verdict(**overrides: Any) -> CriticVerdict:
    fields: dict[str, Any] = {
        "owner_would_say": True,
        "worth_interrupting": True,
        "guesses_hedged": True,
        "grounded": True,
        "adds_information": True,
        "on_topic": True,
        "confidence": 0.9,
    }
    fields.update(overrides)
    return CriticVerdict(**fields)


class _Agent:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def ainvoke(self, payload: Any, config: Any = None, **kwargs: Any) -> dict[str, Any]:
        del payload, config, kwargs
        return {"messages": [AIMessage(content=self._reply)]}

    def astream(self, payload: Any, config: Any = None, **kwargs: Any) -> Any:
        del payload, config, kwargs

        async def chunks() -> Any:
            yield AIMessageChunk(content=self._reply), {"langgraph_node": "model"}

        return chunks()


class _Critic:
    def __init__(self, verdict: CriticVerdict | Exception) -> None:
        self._verdict = verdict
        self.calls: list[dict[str, Any]] = []

    async def judge(
        self,
        *,
        claims: Sequence[CriticClaim],
        sources: Sequence[BookEntry],
        trigger_text: str,
        recent_lines: Sequence[str] = (),
    ) -> CriticVerdict:
        self.calls.append(
            {
                "claims": list(claims),
                "sources": list(sources),
                "trigger_text": trigger_text,
                "recent_lines": list(recent_lines),
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


def _responder(
    critic: _Critic | None, journal: RecordingJournal, plan: str = FACT_PLAN
) -> MeetingResponder:
    return MeetingResponder(
        cast(MeetingAgent, _Agent(plan)),
        NoHistory(),
        journal=journal,
        language="en",
        book_source=BookSource(
            registry=source_registry(
                owner_model={
                    "om-beta": OwnerModelSource(
                        "The Orchid beta launch is planned for 14 October 2026",
                        "room_speakable",
                        kind="position",
                    )
                }
            )
        ),
        critic=cast(Any, critic),
    )


async def test_the_critic_refuses_an_ungrounded_contribution() -> None:
    journal = RecordingJournal()
    critic = _Critic(_verdict(grounded=False))
    chunk = await _responder(critic, journal).respond([_attention()], lambda: None)

    assert chunk.text == ""
    assert journal.events("contribution_dropped")[0]["reason"] == "not_grounded"
    verdict = journal.events("critic_decided")[0]
    assert verdict["refused"] is True
    assert verdict["reason"] == "not_grounded"
    assert verdict["verdict"]["grounded"] is False


async def test_the_critic_judges_the_claims_and_the_sources_they_cite() -> None:
    journal = RecordingJournal()
    critic = _Critic(_verdict())
    chunk = await _responder(critic, journal).respond([_attention()], lambda: None)

    assert chunk.text == "The beta actually launches on 14 October."
    call = critic.calls[0]
    assert [(item.header.act, item.header.refs) for item in call["claims"]] == [
        ("fact", ("om-beta",))
    ]
    assert call["claims"][0].say == "The beta actually launches on 14 October."
    assert call["sources"][0].text.endswith("14 October 2026")
    assert call["trigger_text"] == "Launch Timeline: beta slips to 20 October"
    assert journal.events("critic_decided")[0]["refused"] is False
    assert journal.events("contribution_verified")


class _FactRefusingCritic(_Critic):
    @override
    async def judge(
        self,
        *,
        claims: Sequence[CriticClaim],
        sources: Sequence[BookEntry],
        trigger_text: str,
        recent_lines: Sequence[str] = (),
    ) -> CriticVerdict:
        await super().judge(
            claims=claims, sources=sources, trigger_text=trigger_text, recent_lines=recent_lines
        )
        if any(item.header.act == "fact" for item in claims):
            return _verdict(grounded=False)
        return _verdict()


async def test_a_refused_claim_drops_only_that_claim_from_a_contribution() -> None:
    journal = RecordingJournal()
    plan = plan_reply(
        claim("The beta actually launches on 14 October.", "om-beta"),
        claim(
            "I would hold the launch until the migration is done.",
            "om-beta",
            act="opinion",
        ),
        understood="screen changed",
    )
    critic = _FactRefusingCritic(_verdict())
    chunk = await _responder(critic, journal, plan).respond([_attention()], lambda: None)

    assert chunk.text == "I would hold the launch until the migration is done."
    assert [len(call["claims"]) for call in critic.calls] == [1, 1]
    assert journal.events("contribution_dropped") == []
    verified = journal.events("contribution_verified")[0]
    assert [item["act"] for item in verified["claims"]] == ["opinion"]


async def test_an_owner_position_backed_contribution_is_accepted() -> None:
    journal = RecordingJournal()
    critic = _Critic(_verdict())
    chunk = await _responder(critic, journal, POSITION_PLAN).respond([_attention()], lambda: None)

    assert chunk.text == "I would hold the launch until the migration is done."
    assert critic.calls[0]["claims"][0].header.act == "opinion"
    assert journal.events("contribution_verified")


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        (_verdict(owner_would_say=False), "not_owner_would_say"),
        (_verdict(guesses_hedged=False), "guess_not_hedged"),
        (_verdict(on_topic=False), "off_topic"),
        (_verdict(worth_interrupting=False), "not_worth_interrupting"),
        (_verdict(confidence=0.2), "low_confidence"),
    ],
)
async def test_every_refusal_reason_drops_the_contribution(
    verdict: CriticVerdict, reason: str
) -> None:
    journal = RecordingJournal()
    chunk = await _responder(_Critic(verdict), journal).respond([_attention()], lambda: None)

    assert chunk.text == ""
    assert journal.events("contribution_dropped")[0]["reason"] == reason


async def test_the_critic_is_not_called_for_a_plain_factual_answer() -> None:
    journal = RecordingJournal()
    critic = _Critic(_verdict())
    chunk = await _responder(critic, journal, FACT_ANSWER).respond([event("t1")], lambda: None)

    assert chunk.text == "The launch is on 14 October."
    assert critic.calls == []
    assert journal.events("critic_decided") == []


async def test_a_refused_opinion_falls_back_to_the_unsure_line() -> None:
    journal = RecordingJournal()
    critic = _Critic(_verdict(owner_would_say=False))
    chunk = await _responder(critic, journal, POSITION_PLAN).respond([event("t1")], lambda: None)

    assert chunk.text == UNSURE
    assert critic.calls[0]["claims"][0].header.act == "opinion"
    verdict = journal.events("critic_decided")[0]
    assert verdict["refused"] is True
    assert verdict["reason"] == "not_owner_would_say"
    assert journal.events("speech_rejected")[0]["reason"] == "critic_refused"


async def test_the_streaming_path_reviews_the_contribution_before_speaking() -> None:
    journal = RecordingJournal()
    critic = _Critic(_verdict())
    responder = _responder(critic, journal)

    chunks = [
        chunk
        async for chunk in responder.stream_sentences([_attention()], lambda: None, request_id="r1")
    ]

    assert [chunk.text for chunk in chunks] == ["The beta actually launches on 14 October."]
    assert len(critic.calls) == 1


async def test_the_streaming_path_drops_a_refused_contribution() -> None:
    journal = RecordingJournal()
    critic = _Critic(_verdict(adds_information=False))
    responder = _responder(critic, journal)

    chunks = [
        chunk
        async for chunk in responder.stream_sentences([_attention()], lambda: None, request_id="r1")
    ]

    assert [chunk.text for chunk in chunks] == []
    assert journal.events("contribution_dropped")[0]["reason"] == "adds_nothing"


async def test_the_streaming_path_waits_for_the_critic_on_an_opinion() -> None:
    journal = RecordingJournal()
    critic = _Critic(_verdict())
    responder = _responder(critic, journal, POSITION_PLAN)

    chunks = [
        chunk
        async for chunk in responder.stream_sentences([event("t1")], lambda: None, request_id="r1")
    ]

    assert [chunk.text for chunk in chunks] == [
        "I would hold the launch until the migration is done."
    ]
    assert critic.calls[0]["claims"][0].header.act == "opinion"


async def test_a_failed_critic_call_stays_silent() -> None:
    journal = RecordingJournal()
    chunk = await _responder(_Critic(RuntimeError("down")), journal).respond(
        [_attention()], lambda: None
    )

    assert chunk.text == ""
    assert journal.events("contribution_dropped")[0]["reason"] == "critic_failed"
