import asyncio
from collections.abc import AsyncIterator, Callable, Sequence

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.foundation.transcript import TranscriptEvent


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
    ) -> AsyncIterator[SpokenChunk]:
        del on_tool_start
        del request_id
        self.calls.append(list(events))
        return self._generate()

    async def _generate(self) -> AsyncIterator[SpokenChunk]:
        for sentence in self.sentences:
            yield SpokenChunk(text=sentence, language=DEFAULTS.speech.language)
