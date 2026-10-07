from fakeyou.utterance_quality import normalize_words

TERMINAL_MARKS = frozenset({".", "?", "!"})


class SemanticEndpoint:
    def __init__(self, min_words: int = 3) -> None:
        self._min_words = min_words
        self._previous: tuple[str, ...] | None = None
        self._previous_terminal = False
        self._fired = False

    def reset(self) -> None:
        self._previous = None
        self._previous_terminal = False
        self._fired = False

    def observe(self, text: str) -> bool:
        words = normalize_words(text)
        terminal = text.strip().endswith(tuple(TERMINAL_MARKS))
        complete = (
            terminal
            and len(words) >= self._min_words
            and self._previous is not None
            and self._previous_terminal
            and words == self._previous
        )
        self._previous = words
        self._previous_terminal = terminal
        if not complete:
            self._fired = False
            return False
        if self._fired:
            return False
        self._fired = True
        return True
