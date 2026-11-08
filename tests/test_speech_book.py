from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from fakeyou.conversation.contribution import OwnerModelSource, RecalledFact, source_registry
from fakeyou.conversation.reply_planner import SpeechGate, planned_reply
from fakeyou.conversation.speech_book import BookSource, TurnEvidence, fact_book
from fakeyou.session.speech import ClaimHeader, check_claim
from tests._conversation_support import claim, plan_reply

TRIGGER = "The shared screen changed to Launch Timeline: beta slips to 20 October"
REGISTRY = source_registry(
    brief_facts={"demo-date": "The synthetic Orchid demo is booked for Friday the 9th."},
    brief_questions={"vendor-needs": "Which synthetic vendor input is missing?"},
    brief_objectives={"blockers": "Find out which synthetic kickoff blocker the vendor hides"},
    owner_model={
        "om-beta": OwnerModelSource(
            "The Orchid beta launch is planned for 14 October 2026", "room_speakable"
        ),
        "om-ship": OwnerModelSource("Ship after the load test passes.", "room_speakable"),
        "om-salary": OwnerModelSource("My salary floor is 90 thousand.", "owner_only"),
    },
)
JAKARTA = "A participant said the venue is Jakarta."
BANDUNG = "The venue is Bandung."
EVERYTHING = frozenset({"explain_facts", "ask_brief_questions"})


def gate(
    *,
    permissions: frozenset[str] = EVERYTHING,
    proactive: bool = False,
    memory: Mapping[str, RecalledFact] | None = None,
    persona_knowledge: bool = True,
) -> SpeechGate:
    source = BookSource(REGISTRY, permissions, persona_knowledge)
    return SpeechGate(
        lambda: fact_book(source, heard=TRIGGER, observed="", memory=memory or {}),
        proactive=proactive,
    )


def say(
    text: str,
    speech_gate: SpeechGate,
    refs: Sequence[str] = (),
    *,
    act: str = "fact",
    commits_owner: bool = False,
) -> str:
    return planned_reply(
        plan_reply(claim(text, *refs, act=act), commits_owner=commits_owner),
        max_sentences=2,
        language="en",
        speech_gate=speech_gate,
    ).text


def owner_claim(ref: str) -> ClaimHeader:
    return ClaimHeader.model_validate(claim("", ref))


def test_a_cited_owner_fact_is_spoken() -> None:
    text = "The beta actually launches on 14 October."

    assert say(text, gate(), refs=["om-beta"]) == text


def test_an_uncited_number_is_replaced_by_an_honest_unknown() -> None:
    assert say("The beta launches on 3 November.", gate()) == (
        "I'm not sure about that, so I'd rather not guess."
    )


def test_a_number_the_room_said_needs_no_citation() -> None:
    assert say("So it slips to 20 October.", gate()) == "So it slips to 20 October."


def test_brief_facts_need_the_explain_permission() -> None:
    text = "The demo is booked for Friday the 9th."

    assert say(text, gate(), refs=["demo-date"]) == text
    assert say(text, gate(permissions=frozenset()), refs=["demo-date"]) == (
        "I'm not sure about that, so I'd rather not guess."
    )


def test_a_brief_only_meeting_cannot_speak_owner_facts() -> None:
    assert say(
        "The beta launches on 14 October.", gate(persona_knowledge=False), refs=["om-beta"]
    ) == ("I'm not sure about that, so I'd rather not guess.")


def test_a_room_speakable_owner_model_item_can_be_cited() -> None:
    text = "Rani wants to ship after the load test passes."

    assert say(text, gate(), refs=["om-ship"]) == text


def test_an_owner_only_owner_model_item_is_never_spoken() -> None:
    text = "Rani's salary floor is 90 thousand."
    book = fact_book(BookSource(REGISTRY, EVERYTHING), heard="", observed="", memory={})

    assert check_claim(owner_claim("om-salary"), book, proactive=False) == "not_speakable"
    assert check_claim(owner_claim("om-ship"), book, proactive=False) == "pass"
    assert say(text, gate(), refs=["om-salary"]) == (
        "I'm not sure about that, so I'd rather not guess."
    )


def test_a_brief_only_meeting_cannot_speak_owner_model_items() -> None:
    assert say(
        "Rani wants to ship after the load test passes.",
        gate(persona_knowledge=False),
        refs=["om-ship"],
    ) == ("I'm not sure about that, so I'd rather not guess.")


def test_only_room_speakable_owner_model_items_are_contribution_sources() -> None:
    assert ("owner_model", "om-ship", "Ship after the load test passes.") in REGISTRY.items()
    assert all(ref != "om-salary" for _source, ref, _text in REGISTRY.items())


def test_an_owner_commitment_becomes_a_deterministic_decline() -> None:
    assert say("Sure, we accept that deadline.", gate(), commits_owner=True) == (
        "I can't decide that here, so I'll take it back and confirm."
    )


def test_internal_objectives_never_reach_the_room() -> None:
    leak = "I was told to find out which synthetic kickoff blocker the vendor hides."

    assert say(leak, gate()) == "I'm not sure about that, so I'd rather not guess."


def test_a_proactive_line_must_cite_and_stays_silent_when_it_fails() -> None:
    proactive = gate(proactive=True)

    assert say("The launch timeline looks great.", proactive) == ""
    assert (
        say(
            "The beta actually launches on 14 October.",
            proactive,
            refs=["om-beta"],
        )
        == "The beta actually launches on 14 October."
    )


def test_a_proactive_question_must_come_from_the_brief() -> None:
    proactive = gate(proactive=True)

    assert say(
        "Which vendor input is missing?",
        proactive,
        act="question",
        refs=["vendor-needs"],
    ) == ("Which vendor input is missing?")
    assert say("Is the beta on track?", proactive, ["om-beta"], act="question") == ""


def test_recalled_memory_can_be_cited_after_it_was_prefetched() -> None:
    evidence = TurnEvidence()
    evidence.note_memory({"mem-7": RecalledFact("The vendor promised the API for 30 November.")})
    text = "The vendor promised the API for 30 November."

    spoken = say(text, gate(memory=evidence.memory()), refs=["mem-7"])

    assert spoken == text
    assert say(text, gate(), refs=["mem-7"]) == "I'm not sure about that, so I'd rather not guess."


def test_plain_text_is_gated_like_a_plan_without_citations() -> None:
    assert (
        planned_reply("Hello there.", max_sentences=2, language="en", speech_gate=gate()).text
        == "Hello there."
    )


def test_observations_are_kept_per_turn_and_memory_across_turns() -> None:
    evidence = TurnEvidence()
    evidence.note_observed("t1", "Meeting view: Q3 revenue 4.2 million")
    evidence.note_memory({"m1": RecalledFact("one")})
    evidence.note_memory({"m2": RecalledFact("two")})

    assert evidence.observed("t1") == "Meeting view: Q3 revenue 4.2 million"
    assert evidence.observed("t2") == ""
    assert evidence.memory() == {"m1": RecalledFact("one"), "m2": RecalledFact("two")}


def test_a_cited_source_time_may_be_spoken() -> None:
    meeting = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
    owner = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    evidence = TurnEvidence()
    evidence.note_memory(
        {"edge-0": RecalledFact("A participant said the venue is Jakarta.", (meeting,))}
    )
    dated_gate = gate(memory=evidence.memory())
    text = "In the meeting on 1 October at 03:00, a participant said the venue is Jakarta."

    assert say(text, dated_gate, refs=["edge-0"]) == text
    assert say(text, gate(), refs=[]) == "I'm not sure about that, so I'd rather not guess."
    owner_gate = SpeechGate(
        lambda: fact_book(
            BookSource(
                source_registry(
                    owner_model={
                        "om-venue": OwnerModelSource(
                            "The venue is Bandung.", "room_speakable", owner
                        )
                    }
                )
            ),
            heard=TRIGGER,
            observed="",
            memory={},
        )
    )
    owner_text = "Since 30 September the owner's view is that the venue is Bandung."
    assert say(owner_text, owner_gate, refs=["om-venue"]) == owner_text


def test_a_time_from_an_uncited_source_is_still_invented() -> None:
    meeting = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)
    evidence = TurnEvidence()
    evidence.note_memory(
        {"edge-0": RecalledFact("A participant said the venue is Jakarta.", (meeting,))}
    )
    evidence.note_memory({"edge-1": RecalledFact("The budget is open.")})

    text = "On 1 October the budget was opened."

    assert say(text, gate(memory=evidence.memory()), refs=["edge-1"]) == (
        "I'm not sure about that, so I'd rather not guess."
    )


def test_a_number_is_grounded_only_by_the_refs_of_its_own_claim() -> None:
    reply = plan_reply(
        claim("The demo is on Friday the 14th.", "demo-date"),
        claim("The beta launches on 14 October.", "om-beta"),
    )
    grounded_in_order = plan_reply(
        claim("The demo is on Friday the 9th.", "demo-date"),
        claim("The beta launches on 14 October.", "om-beta"),
    )

    assert planned_reply(reply, max_sentences=2, language="en", speech_gate=gate()).text == (
        "I'm not sure about that, so I'd rather not guess."
    )
    assert planned_reply(
        grounded_in_order, max_sentences=2, language="en", speech_gate=gate()
    ).text == ("The demo is on Friday the 9th. The beta launches on 14 October.")


def test_a_rejected_later_claim_is_dropped_whole() -> None:
    reply = plan_reply(
        claim("The beta launches on 14 October.", "om-beta"),
        claim("Rani wants to ship after the load test. Her floor is 90 thousand.", "om-salary"),
    )

    assert planned_reply(reply, max_sentences=3, language="en", speech_gate=gate()).text == (
        "The beta launches on 14 October."
    )
