from fakeyou.foundation.participation import (
    ParticipationContext,
    choose_participation,
    is_vocative,
    same_observed_speaker,
)


def test_explicit_address_unknown_speaker_replies():
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


def test_explicit_address_ambiguous_clarifies():
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


def test_question_for_another_participant_stays_silent():
    context = ParticipationContext(
        addressed=False,
        followup=False,
        same_participant=False,
        ambiguous=False,
        others_speaking=False,
        activation_valid=False,
    )
    assert choose_participation(context).action == "silent"


def test_twin_mentioned_in_quote_stays_silent():
    context = ParticipationContext(
        addressed=False,
        followup=False,
        same_participant=False,
        ambiguous=True,
        others_speaking=False,
        activation_valid=True,
    )
    assert choose_participation(context).action == "silent"


def test_followup_same_speaker_replies():
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


def test_another_speaker_does_not_inherit_activation():
    context = ParticipationContext(
        addressed=False,
        followup=True,
        same_participant=False,
        ambiguous=False,
        others_speaking=False,
        activation_valid=True,
    )
    assert choose_participation(context).action == "silent"


def test_expired_activation_followup_stays_silent():
    context = ParticipationContext(
        addressed=False,
        followup=True,
        same_participant=True,
        ambiguous=False,
        others_speaking=False,
        activation_valid=False,
    )
    assert choose_participation(context).action == "silent"


def test_occupied_floor_stays_silent_even_when_addressed():
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


def test_ambiguous_followup_same_speaker_clarifies():
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


def test_unknown_without_address_stays_silent():
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


def test_same_participant_without_followup_stays_silent():
    context = ParticipationContext(
        addressed=False,
        followup=False,
        same_participant=True,
        ambiguous=False,
        others_speaking=False,
        activation_valid=True,
    )
    assert choose_participation(context).action == "silent"


def test_decision_covers_all_contexts():
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


def test_vocative_matches_name_at_utterance_start():
    assert is_vocative("Rani Twin, when is the demo?", ["Rani Twin"])
    assert is_vocative("rani twin when is the demo?", ["Rani Twin"])
    assert is_vocative("Twin: hello", ["Twin"])
    assert is_vocative("Rani Twin", ["Rani Twin"])
    assert not is_vocative("About Rani Twin yesterday", ["Rani Twin"])
    assert not is_vocative("He said Rani Twin agrees", ["Rani Twin"])
    assert not is_vocative("Rani Twinagrees yes?", ["Rani Twin"])
    assert not is_vocative("Hello", ["Rani Twin"])
    assert not is_vocative("Rani Twin, hello", ["", "  "])


def test_vocative_tries_every_alias():
    assert is_vocative("Twin, hello", ["Rani Twin", "Twin"])
    assert is_vocative("Rani Twin, hello", ["Rani Twin", "Twin"])
    assert not is_vocative("Twinx, hello", ["Rani Twin", "Twin"])


def test_same_observed_speaker_requires_two_equal_ids():
    assert same_observed_speaker("a", "a")
    assert not same_observed_speaker(None, "a")
    assert not same_observed_speaker("a", "b")
    assert not same_observed_speaker("unknown", "unknown")
    assert not same_observed_speaker("a", "unknown")
    assert not same_observed_speaker("unknown", "a")


def test_context_is_immutable():
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
