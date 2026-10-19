import pytest

from fakeyou.perception.scrub import mentions_internal_ref, scrub_internal_refs


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        (
            "Based on screen#1, the beta is on 17 March 2027.",
            "Based on, the beta is on 17 March 2027.",
        ),
        ("The Pro tier is the popular one [screen#2].", "The Pro tier is the popular one."),
        ("Budi asked in chat (chat#4) for the deck.", "Budi asked in chat for the deck."),
        (
            "Frame seen at 2026-10-09T23:29:19.512483+00:00 shows the roadmap.",
            "Frame seen at shows the roadmap.",
        ),
        ("Ask person#3 about it.", "Ask about it."),
    ],
)
def test_internal_references_and_timestamps_never_reach_speech(spoken: str, expected: str) -> None:
    assert scrub_internal_refs(spoken) == expected
    assert mentions_internal_ref(spoken)
    assert not mentions_internal_ref(scrub_internal_refs(spoken))


@pytest.mark.parametrize(
    "spoken",
    [
        "She has an S1 degree and a C5 certificate.",
        "The meeting is at 10:30 on 2027-03-17.",
        "Use the #general channel.",
    ],
)
def test_ordinary_speech_is_left_alone(spoken: str) -> None:
    assert scrub_internal_refs(spoken) == spoken
    assert not mentions_internal_ref(spoken)
