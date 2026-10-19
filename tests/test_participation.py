from fakeyou.foundation.participation import (
    ParticipationContext,
    choose_participation,
    is_other_observed_speaker,
    is_vocative,
    same_observed_speaker,
)


def _followup(
    *, bare_ack: bool = False, interjection: bool = False, awaiting_reply: bool = False
) -> ParticipationContext:
    return ParticipationContext(
        addressed=False,
        followup=True,
        same_participant=True,
        ambiguous=False,
        others_speaking=False,
        activation_valid=True,
        bare_ack=bare_ack,
        interjection=interjection,
        awaiting_reply=awaiting_reply,
    )


def test_interjection_inside_the_window_is_silent_for_the_engaged_speaker() -> None:
    decision = choose_participation(_followup(interjection=True))
    assert (decision.action, decision.reason) == ("silent", "interjection")


def test_bare_ack_after_a_statement_is_silent() -> None:
    decision = choose_participation(_followup(bare_ack=True, awaiting_reply=False))
    assert (decision.action, decision.reason) == ("silent", "bare_ack")


def test_bare_ack_answering_a_question_gets_a_reply() -> None:
    decision = choose_participation(_followup(bare_ack=True, awaiting_reply=True))
    assert (decision.action, decision.reason) == ("reply", "followup_answers_question")


def test_addressed_turn_is_not_silenced_by_content_signals() -> None:
    context = ParticipationContext(
        addressed=True,
        followup=False,
        same_participant=False,
        ambiguous=False,
        others_speaking=False,
        activation_valid=False,
        interjection=True,
        bare_ack=True,
    )
    decision = choose_participation(context)
    assert (decision.action, decision.reason) == ("reply", "addressed")


def test_other_speaker_is_a_known_different_identity() -> None:
    assert is_other_observed_speaker("speaker-a", "speaker-b")
    assert not is_other_observed_speaker("speaker-a", "speaker-a")
    assert not is_other_observed_speaker("speaker-a", "unknown")
    assert not is_other_observed_speaker("unknown", "speaker-b")
    assert not is_other_observed_speaker(None, "speaker-b")


def test_explicit_address_unknown_speaker_replies() -> None:
    context = ParticipationContext(
        addressed=True,
        followup=False,
        same_participant=False,
        ambiguous=False,
        others_speaking=False,
        activation_valid=False,
    )
    decision = choose_participation(context)
    assert decision.action == "reply"
    assert decision.reason == "addressed"


def test_explicit_address_ambiguous_clarifies() -> None:
    context = ParticipationContext(
        addressed=True,
        followup=False,
        same_participant=False,
        ambiguous=True,
        others_speaking=False,
        activation_valid=False,
    )
    decision = choose_participation(context)
    assert decision.action == "clarify"
    assert decision.reason == "addressed_ambiguous"


def test_question_for_another_participant_stays_silent() -> None:
    context = ParticipationContext(
        addressed=False,
        followup=False,
        same_participant=False,
        ambiguous=False,
        others_speaking=False,
        activation_valid=False,
    )
    assert choose_participation(context).action == "silent"


def test_twin_mentioned_in_quote_stays_silent() -> None:
    context = ParticipationContext(
        addressed=False,
        followup=False,
        same_participant=False,
        ambiguous=True,
        others_speaking=False,
        activation_valid=True,
    )
    assert choose_participation(context).action == "silent"


def test_followup_same_speaker_replies() -> None:
    context = ParticipationContext(
        addressed=False,
        followup=True,
        same_participant=True,
        ambiguous=False,
        others_speaking=False,
        activation_valid=True,
    )
    decision = choose_participation(context)
    assert decision.action == "reply"
    assert decision.reason == "followup_same_speaker"


def test_another_speaker_does_not_inherit_activation() -> None:
    context = ParticipationContext(
        addressed=False,
        followup=True,
        same_participant=False,
        ambiguous=False,
        others_speaking=False,
        activation_valid=True,
    )
    assert choose_participation(context).action == "silent"


def test_expired_activation_followup_stays_silent() -> None:
    context = ParticipationContext(
        addressed=False,
        followup=True,
        same_participant=True,
        ambiguous=False,
        others_speaking=False,
        activation_valid=False,
    )
    assert choose_participation(context).action == "silent"


def test_occupied_floor_stays_silent_even_when_addressed() -> None:
    context = ParticipationContext(
        addressed=True,
        followup=False,
        same_participant=False,
        ambiguous=True,
        others_speaking=True,
        activation_valid=False,
    )
    decision = choose_participation(context)
    assert decision.action == "silent"
    assert decision.reason == "floor_occupied"


def test_ambiguous_followup_same_speaker_clarifies() -> None:
    context = ParticipationContext(
        addressed=False,
        followup=True,
        same_participant=True,
        ambiguous=True,
        others_speaking=False,
        activation_valid=True,
    )
    decision = choose_participation(context)
    assert decision.action == "clarify"
    assert decision.reason == "followup_ambiguous"


def test_unknown_without_address_stays_silent() -> None:
    context = ParticipationContext(
        addressed=False,
        followup=False,
        same_participant=False,
        ambiguous=True,
        others_speaking=False,
        activation_valid=False,
    )
    decision = choose_participation(context)
    assert decision.action == "silent"
    assert decision.reason == "not_addressed"


def test_same_participant_without_followup_stays_silent() -> None:
    context = ParticipationContext(
        addressed=False,
        followup=False,
        same_participant=True,
        ambiguous=False,
        others_speaking=False,
        activation_valid=True,
    )
    assert choose_participation(context).action == "silent"


def test_decision_covers_all_contexts() -> None:
    seen = set()
    for raw in range(64):
        flags = [(raw >> bit & 1) == 1 for bit in range(6)]
        context = ParticipationContext(
            addressed=flags[0],
            followup=flags[1],
            same_participant=flags[2],
            ambiguous=flags[3],
            others_speaking=flags[4],
            activation_valid=flags[5],
        )
        before = (
            context.addressed,
            context.followup,
            context.same_participant,
            context.ambiguous,
            context.others_speaking,
            context.activation_valid,
        )
        decision = choose_participation(context)
        assert decision.action in ("reply", "silent", "clarify")
        assert decision.reason
        seen.add(decision.action)
        assert (
            context.addressed,
            context.followup,
            context.same_participant,
            context.ambiguous,
            context.others_speaking,
            context.activation_valid,
        ) == before
    assert seen == {"reply", "silent", "clarify"}


def test_vocative_matches_name_at_utterance_start() -> None:
    assert is_vocative("Rani Twin, when is the demo?", ["Rani Twin"])
    assert is_vocative("rani twin when is the demo?", ["Rani Twin"])
    assert is_vocative("Twin: hello", ["Twin"])
    assert is_vocative("Rani Twin", ["Rani Twin"])
    assert not is_vocative("About Rani Twin yesterday", ["Rani Twin"])
    assert not is_vocative("He said Rani Twin agrees", ["Rani Twin"])
    assert not is_vocative("Rani Twinagrees yes?", ["Rani Twin"])
    assert not is_vocative("Hello", ["Rani Twin"])
    assert not is_vocative("Rani Twin, hello", ["", "  "])


def test_vocative_tries_every_alias() -> None:
    assert is_vocative("Twin, hello", ["Rani Twin", "Twin"])
    assert is_vocative("Rani Twin, hello", ["Rani Twin", "Twin"])
    assert not is_vocative("Twinx, hello", ["Rani Twin", "Twin"])


def test_same_observed_speaker_requires_two_equal_ids() -> None:
    assert same_observed_speaker("a", "a")
    assert not same_observed_speaker(None, "a")
    assert not same_observed_speaker("a", "b")
    assert not same_observed_speaker("unknown", "unknown")
    assert not same_observed_speaker("a", "unknown")
    assert not same_observed_speaker("unknown", "a")


def test_context_is_immutable() -> None:
    context = ParticipationContext(
        addressed=True,
        followup=False,
        same_participant=False,
        ambiguous=False,
        others_speaking=False,
        activation_valid=False,
    )
    field = "addressed"
    try:
        setattr(context, field, False)
    except AttributeError:
        pass
    else:
        raise AssertionError("ParticipationContext must be frozen")
    assert choose_participation(context).action == "reply"


def test_turn_mine_does_not_override_occupied_floor() -> None:
    context = ParticipationContext(
        addressed=False,
        followup=True,
        same_participant=True,
        ambiguous=False,
        others_speaking=True,
        activation_valid=True,
        turn_cue="explicit",
        turn_mine=True,
    )
    assert choose_participation(context).action == "silent"
