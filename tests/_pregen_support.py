import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from fakeyou.foundation.transcript import TranscriptEvent
from tests._persistence_fakes import RecordingJournal


class StreamingResponder:
    def __init__(self, sentences: list[str]) -> None:
        self.sentences = sentences
        self.calls: list[list[TranscriptEvent]] = []
        self.released = asyncio.Event()

    def stream_sentences(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
    ) -> AsyncIterator[str]:
        del on_tool_start
        del request_id
        self.calls.append(list(events))
        return self._generate()

    async def _generate(self) -> AsyncIterator[str]:
        for sentence in self.sentences:
            yield sentence


def journal_has(journal: RecordingJournal, name: str, **fields: Any) -> bool:
    return any(
        all(row.get(key) == value for key, value in fields.items()) for row in journal.events(name)
    )


DIFF_KEYS = ("dist", "words_pre", "words_final", "norm_dist_bucket", "diff_class")
