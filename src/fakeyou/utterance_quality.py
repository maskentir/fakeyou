from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

SCRIPT_WINDOW_BEFORE_SECONDS = 10.0
SCRIPT_WINDOW_AFTER_SECONDS = 30.0


def normalize_words(text: str) -> tuple[str, ...]:
    return tuple(
        cleaned
        for word in text.casefold().split()
        if (cleaned := "".join(char for char in word if char.isalnum()))
    )


def _word_edits(expected: Sequence[str], actual: Sequence[str]) -> int:
    costs = list(range(len(actual) + 1))
    for expected_index, expected_word in enumerate(expected, start=1):
        previous = costs[0]
        costs[0] = expected_index
        for actual_index, actual_word in enumerate(actual, start=1):
            current = costs[actual_index]
            costs[actual_index] = min(
                costs[actual_index] + 1,
                costs[actual_index - 1] + 1,
                previous + (expected_word != actual_word),
            )
            previous = current
    return costs[-1]


def describe_word_diff(old: Sequence[str], new: Sequence[str]) -> dict[str, float | int | str]:
    before = tuple(old)
    after = tuple(new)
    if before == after:
        diff_class = "exact"
    elif len(after) > len(before) and after[: len(before)] == before:
        diff_class = "prefix_extend"
    elif len(before) > len(after) and before[: len(after)] == after:
        diff_class = "prefix_truncate"
    elif (
        len(before) == len(after)
        and sum(first != second for first, second in zip(before, after, strict=True)) == 1
    ):
        diff_class = "single_subst"
    else:
        diff_class = "other"
    dist = _word_edits(before, after)
    size = max(len(before), len(after))
    bucket = round(round(dist / size / 0.05) * 0.05, 2) if size else 0.0
    return {
        "dist": dist,
        "words_pre": len(before),
        "words_final": len(after),
        "norm_dist_bucket": bucket,
        "diff_class": diff_class,
    }


def word_error_rate(expected: str, actual: str) -> float:
    expected_words = normalize_words(expected)
    actual_words = normalize_words(actual)
    if not expected_words:
        return 0.0 if not actual_words else 1.0
    return _word_edits(expected_words, actual_words) / len(expected_words)


def score_pairs(pairs: Sequence[tuple[str, str]]) -> dict[str, float | int]:
    edits = 0
    words = 0
    exact = 0
    for expected, actual in pairs:
        expected_words = normalize_words(expected)
        actual_words = normalize_words(actual)
        edits += _word_edits(expected_words, actual_words)
        words += len(expected_words)
        exact += expected_words == actual_words
    return {
        "pairs": len(pairs),
        "exact_matches": exact,
        "exact_match_rate": exact / len(pairs) if pairs else 0.0,
        "word_error_rate": edits / words if words else 0.0,
    }


def _moment(value: Any) -> float:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def score_script(
    script: Sequence[Mapping[str, Any]],
    transcripts: Sequence[Mapping[str, Any]],
    before_seconds: float = SCRIPT_WINDOW_BEFORE_SECONDS,
    after_seconds: float = SCRIPT_WINDOW_AFTER_SECONDS,
) -> dict[str, Any]:
    candidates = (
        (str(entry.get("text") or ""), _moment(entry.get("spoken_at"))) for entry in transcripts
    )
    spoken = sorted(
        ((heard, at) for heard, at in candidates if normalize_words(heard)),
        key=lambda item: item[1],
    )
    matched: list[tuple[str, str]] = []
    misses: list[str] = []
    for entry in script:
        text = str(entry.get("text") or "")
        moment = _moment(entry.get("at"))
        hit = next(
            (
                heard
                for heard, at in spoken
                if moment - before_seconds <= at <= moment + after_seconds
            ),
            None,
        )
        if hit is None:
            misses.append(text)
        else:
            matched.append((text, hit))
    pairs = score_pairs(matched)
    return {
        "scripted": len(script),
        "covered": len(matched),
        "misses": misses,
        "exact_matches": pairs["exact_matches"],
        "word_error_rate": pairs["word_error_rate"],
    }
