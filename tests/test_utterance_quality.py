import json
from pathlib import Path

from fakeyou.utterance_quality import score_pairs, score_script, word_error_rate

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "short-utterances.json"


def test_word_error_rate_counts_word_edits() -> None:
    assert word_error_rate("hello world", "hello world") == 0.0
    assert word_error_rate("hello world", "hello there") == 0.5
    assert word_error_rate("hello world", "hello") == 0.5
    assert word_error_rate("hello", "hello brave world") == 2.0
    assert word_error_rate("", "") == 0.0
    assert word_error_rate("", "hello") == 1.0


def test_word_error_rate_ignores_case_and_punctuation() -> None:
    assert word_error_rate("Hello, brave world!", "hello brave world") == 0.0


def test_score_pairs_reports_exact_matches_and_micro_average() -> None:
    report = score_pairs(
        [("hello world", "hello world"), ("good morning", "good evening"), ("yes", "")]
    )

    assert report["pairs"] == 3
    assert report["exact_matches"] == 1
    assert report["exact_match_rate"] == 1 / 3
    assert report["word_error_rate"] == 0.4


def test_score_script_matches_transcripts_inside_time_windows() -> None:
    script = [
        {"text": "hello world", "at": "2026-10-06T01:00:10+00:00"},
        {"text": "good morning", "at": "2026-10-06T01:00:40+00:00"},
        {"text": "yes please", "at": "2026-10-06T01:05:00+00:00"},
    ]
    transcripts = [
        {"text": "hello world", "spoken_at": "2026-10-06T01:00:12+00:00"},
        {"text": "good evening", "spoken_at": "2026-10-06T01:00:45+00:00"},
        {"text": "stray words", "spoken_at": "2026-10-06T01:03:00+00:00"},
    ]

    report = score_script(script, transcripts)

    assert report["scripted"] == 3
    assert report["covered"] == 2
    assert report["misses"] == ["yes please"]
    assert report["exact_matches"] == 1
    assert report["word_error_rate"] == 0.25


def test_score_script_prefers_earliest_transcript_in_window() -> None:
    script = [{"text": "zebra early hello", "at": "2026-10-06T01:00:10+00:00"}]
    transcripts = [
        {"text": "apple late hello", "spoken_at": "2026-10-06T01:00:11+00:00"},
        {"text": "zebra early hello", "spoken_at": "2026-10-06T01:00:09+00:00"},
    ]

    report = score_script(script, transcripts)

    assert report["covered"] == 1
    assert report["exact_matches"] == 1
    assert report["word_error_rate"] == 0.0


def test_fixture_cases_score_zero_against_themselves() -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    pairs = [(case["expected"], case["expected"]) for case in cases]

    report = score_pairs(pairs)

    assert report["pairs"] == len(cases)
    assert report["exact_matches"] == len(cases)
    assert report["word_error_rate"] == 0.0


def test_short_utterance_fixture_covers_digits_and_backchannels() -> None:
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]

    assert len(cases) == 8
    assert {case["language"] for case in cases} == {"id", "en"}
    assert all(case["expected"].strip() == case["expected"] and case["expected"] for case in cases)
    assert sum(len(case["expected"].split()) == 1 for case in cases) == 6
