from fakeyou.foundation.transcript import spoken_part, with_system_notes


def test_system_notes_follow_the_spoken_line_and_can_be_removed() -> None:
    noted = with_system_notes("When is the beta?", ["Answer in English.", "Be brief."])

    assert noted == "When is the beta?\n[System: Answer in English. Be brief.]"
    assert spoken_part(noted) == "When is the beta?"


def test_a_line_without_notes_is_unchanged() -> None:
    assert with_system_notes("Hello.", []) == "Hello."
    assert spoken_part("Hello.") == "Hello."
