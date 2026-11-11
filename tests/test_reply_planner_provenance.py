from typing import cast

import pytest

from fakeyou.conversation.reply_planner import ReplyHandlers, ReplyPlanner, SpeechGate
from fakeyou.session.facts import Disclosure
from fakeyou.session.speech import BookEntry, FactBook
from tests._conversation_support import claim, plan_reply


@pytest.mark.parametrize(
    ("text", "disclosure", "proactive", "verification", "authorized"),
    [
        ("What does the vendor need?", "room_speakable", False, (True, "q1"), True),
        ("Anything else?", "room_speakable", False, (True, "q1"), False),
        ("What does the vendor need?", "owner_only", False, (True, "q1"), False),
        ("What does the vendor need?", "room_speakable", True, (True, "q1"), False),
        ("What does the vendor need?", "room_speakable", False, (False, "q1"), False),
        ("What does the vendor need?", "room_speakable", False, (True, "unknown"), False),
    ],
)
def test_only_verified_permitted_brief_question_earns_an_exception(
    *, text: str, disclosure: str, proactive: bool, verification: tuple[bool, str], authorized: bool
) -> None:
    verify, ref = verification
    accepted: list[tuple[int, str, bool]] = []
    entry = BookEntry(
        "q1", "What does the vendor need?", "brief", cast(Disclosure, disclosure), kind="question"
    )
    planner = ReplyPlanner(
        max_sentences=2,
        language="en",
        speech_gate=SpeechGate(lambda: FactBook({"q1": entry}), proactive=proactive, verify=verify),
        handlers=ReplyHandlers(
            on_sentence=lambda index, sentence, allowed: accepted.append((index, sentence, allowed))
        ),
    )
    planner.speak(plan_reply(claim(text, ref, act="question")))

    assert any(allowed for _, _, allowed in accepted) is authorized


def test_extra_sentence_in_the_same_claim_is_not_authorized() -> None:
    accepted: list[tuple[int, str, bool]] = []
    entry = BookEntry("q1", "What does the vendor need?", "brief", kind="question")
    planner = ReplyPlanner(
        max_sentences=2,
        language="en",
        speech_gate=SpeechGate(lambda: FactBook({"q1": entry})),
        handlers=ReplyHandlers(
            on_sentence=lambda index, sentence, allowed: accepted.append((index, sentence, allowed))
        ),
    )
    spoken = planner.speak(
        plan_reply(claim("What does the vendor need? Anything else?", "q1", act="question"))
    )

    assert spoken == ["What does the vendor need?", "Anything else?"]
    assert accepted == [(0, spoken[0], True), (1, spoken[1], False)]


@pytest.mark.parametrize("question", ["What does the vendor need?", "What does the  vendor need?"])
async def test_whole_reply_journals_authorization_for_the_combined_clip(question: str) -> None:
    import hashlib

    from langchain_core.messages import AIMessage

    from fakeyou.conversation.agent import create_meeting_agent
    from fakeyou.conversation.contribution import SourceRegistry
    from fakeyou.conversation.conversation import MeetingResponder, cap_spoken_reply
    from fakeyou.conversation.meeting_messages import NoHistory
    from fakeyou.conversation.speech_book import BookSource
    from tests._conversation_fakes import ToolCallingFakeModel
    from tests._conversation_support import event
    from tests._persistence_fakes import RecordingJournal

    journal = RecordingJournal()
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content=plan_reply(
                    claim("The deadline is Friday.", "f1"),
                    claim(question, "q1", act="question"),
                    claim("The deadline is Friday.", "f1"),
                )
            )
        ]
    )
    responder = MeetingResponder(
        create_meeting_agent(model),
        NoHistory(),
        journal=journal,
        book_source=BookSource(
            registry=SourceRegistry(
                facts={"f1": "The deadline is Friday."},
                questions={"q1": "What does the vendor need?"},
            ),
            permissions=frozenset({"explain_facts", "ask_brief_questions"}),
        ),
    )
    result = await responder.respond([event("t1", "What should we discuss?")], lambda: None)
    capped = cap_spoken_reply(result.text, 2, "en")
    digest = hashlib.sha256(" ".join(capped.split()).encode()).hexdigest()
    assert " ".join(capped.split()) == "The deadline is Friday. What does the vendor need?"
    assert result.text.endswith("The deadline is Friday.")
    assert any(
        item["sentence_index"] == 0
        and item["sentence_sha256"] == digest
        and item["authorized_brief_question"] is True
        for item in journal.events("answer_sentence_accepted")
    )
