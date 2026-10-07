from fakeyou.utterance_quality import normalize_words

TERMINAL_MARKS = frozenset({".", "?", "!"})


class SemanticEndpoint:
    def __init__(self, min_words: int = 4, stability: int = 3) -> None:
        self._min_words = min_words
        self._stability = stability
        self._previous: tuple[str, ...] | None = None
        self._previous_terminal = False
        self._run = 0
        self._fired = False

    def reset(self) -> None:
        self._previous = None
        self._previous_terminal = False
        self._run = 0
        self._fired = False

    def observe(self, text: str) -> bool:
        words = normalize_words(text)
        terminal = text.strip().endswith(tuple(TERMINAL_MARKS))
        if self._previous is not None and words == self._previous:
            self._run += 1
        else:
            self._run = 1
            self._fired = False
        fast = (
            terminal
            and self._previous_terminal
            and self._run >= 2
            and len(words) >= self._min_words
        )
        stable = self._run >= self._stability and len(words) >= self._min_words
        self._previous = words
        self._previous_terminal = terminal
        if not (fast or stable):
            return False
        if self._fired:
            return False
        self._fired = True
        return True
