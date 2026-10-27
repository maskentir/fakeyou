from fakeyou.session.numbers import keyword_table, numbers_in


def test_digit_numbers_are_found() -> None:
    assert numbers_in("120 juta rupiah") == frozenset({"120"})


def test_english_number_words_are_found() -> None:
    assert numbers_in("the line forty two") == frozenset({"40", "42"})


def test_indonesian_number_words_are_found() -> None:
    for word in keyword_table("id"):
        assert numbers_in(word), word


def test_the_keyword_table_lists_words_for_the_stt() -> None:
    assert "twenty" in keyword_table("en")
    assert keyword_table("ja") == ()
