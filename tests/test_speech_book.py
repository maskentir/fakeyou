import json
from collections.abc import Mapping

from fakeyou.conversation.contribution import source_registry
from fakeyou.conversation.reply_planner import SpeechGate, planned_reply
from fakeyou.conversation.speech_book import BookSource, TurnEvidence, fact_book

TRIGGER = "The shared screen changed to Launch Timeline: beta slips to 20 October"
REGISTRY = source_registry(
    profile_facts=["The Orchid beta launch is planned for 14 October 2026"],
    brief_facts={"demo-date": "The synthetic Orchid demo is booked for Friday the 9th."},
    brief_questions={"vendor-needs": "Which synthetic vendor input is missing?"},
    brief_objectives={"blockers": "Find out which synthetic kickoff blocker the vendor hides"},
)
EVERYTHING = frozenset({"explain_facts", "ask_brief_questions"})


def gate(
    *,
    permissions: frozenset[str] = EVERYTHING,
    proactive: bool = False,
    memory: Mapping[str, str] | None = None,
    persona_knowledge: bool = True,
) -> SpeechGate:
    source = BookSource(REGISTRY, permissions, persona_knowledge)
    return SpeechGate(
        lambda: fact_book(source, heard=TRIGGER, observed="", memory=memory or {}),
        proactive=proactive,
    )


def plan(text: str, **fields: object) -> str:
    return json.dumps({"language": "en", "intent": "answer", **fields, "text": text})


def say(text: str, speech_gate: SpeechGate, **fields: object) -> str:
    return planned_reply(
        plan(text, **fields), max_sentences=2, language="en", speech_gate=speech_gate
    ).text


def test_a_cited_profile_fact_is_spoken() -> None:
    text = "The beta actually launches on 14 October."

    assert say(text, gate(), cites=["fact-0"]) == text


def test_an_uncited_number_is_replaced_by_an_honest_unknown() -> None:
    assert say("The beta launches on 3 November.", gate()) == (
        "I'm not sure about that, so I'd rather not guess."
    )


def test_a_number_the_room_said_needs_no_citation() -> None:
    assert say("So it slips to 20 October.", gate()) == "So it slips to 20 October."


def test_brief_facts_need_the_explain_permission() -> None:
    text = "The demo is booked for Friday the 9th."

    assert say(text, gate(), cites=["demo-date"]) == text
    assert say(text, gate(permissions=frozenset()), cites=["demo-date"]) == (
        "I'm not sure about that, so I'd rather not guess."
    )


def test_a_brief_only_meeting_cannot_speak_profile_facts() -> None:
    assert say(
        "The beta launches on 14 October.", gate(persona_knowledge=False), cites=["fact-0"]
    ) == ("I'm not sure about that, so I'd rather not guess.")


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
            contribution="correction",
            cites=["fact-0"],
        )
        == "The beta actually launches on 14 October."
    )


def test_a_proactive_question_must_come_from_the_brief() -> None:
    proactive = gate(proactive=True)

    assert say(
        "Which vendor input is missing?",
        proactive,
        contribution="question",
        cites=["vendor-needs"],
    ) == ("Which vendor input is missing?")
    assert say("Is the beta on track?", proactive, contribution="question", cites=["fact-0"]) == ""


def test_recalled_memory_can_be_cited_after_it_was_prefetched() -> None:
    evidence = TurnEvidence()
    evidence.note_memory({"mem-7": "The vendor promised the API for 30 November."})
    text = "The vendor promised the API for 30 November."

    spoken = say(text, gate(memory=evidence.memory()), cites=["mem-7"])

    assert spoken == text
    assert say(text, gate(), cites=["mem-7"]) == "I'm not sure about that, so I'd rather not guess."


def test_plain_text_is_gated_like_a_plan_without_citations() -> None:
    assert (
        planned_reply("Hello there.", max_sentences=2, language="en", speech_gate=gate()).text
        == "Hello there."
    )


def test_observations_are_kept_per_turn_and_memory_across_turns() -> None:
    evidence = TurnEvidence()
    evidence.note_observed("t1", "Meeting view: Q3 revenue 4.2 million")
    evidence.note_memory({"m1": "one"})
    evidence.note_memory({"m2": "two"})

    assert evidence.observed("t1") == "Meeting view: Q3 revenue 4.2 million"
    assert evidence.observed("t2") == ""
    assert evidence.memory() == {"m1": "one", "m2": "two"}
