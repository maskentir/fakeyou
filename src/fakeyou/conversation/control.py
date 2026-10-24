from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class ReplyControl:
    sentence_cap: int | None = None
    gain_steps: int = 0
    on_louder: Callable[[], None] | None = None

    def limit(self, sentences: int) -> int:
        if self.sentence_cap is None:
            return sentences
        return min(sentences, self.sentence_cap)
