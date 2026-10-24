from collections.abc import AsyncIterator

from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.foundation.sentences import sentence_end


class SentenceStream:
    def __init__(self, max_sentences: int, language: str, boundary: int = 0) -> None:
        if max_sentences <= 0:
            raise ValueError("max_sentences must be positive")
        self.max_sentences = max_sentences
        self.language = language
        self.buffer = ""
        self.yielded = 0
        self.capped = False
        self._boundary = boundary

    def observe_boundary(self, boundary: int) -> None:
        if boundary != self._boundary:
            self._boundary = boundary
            self.buffer = ""

    def observe_language(self, language: str) -> None:
        if language:
            self.language = language

    def take(self, piece: str) -> list[str]:
        self.buffer += piece
        sentences: list[str] = []
        while self.yielded < self.max_sentences:
            boundary = sentence_end(self.buffer, language=self.language)
            if not boundary:
                break
            sentence = self.buffer[:boundary].strip()
            self.buffer = self.buffer[boundary:].lstrip()
            if not sentence:
                continue
            self.yielded += 1
            sentences.append(sentence)
        return sentences

    def check_cap(self) -> bool:
        if self.yielded >= self.max_sentences and self.buffer.strip():
            self.capped = True
            return True
        return False

    def flush(self) -> str | None:
        if not self.capped and self.yielded < self.max_sentences and self.buffer.strip():
            return self.buffer.strip()
        return None


async def shape_sentences(
    chunks: AsyncIterator[SpokenChunk], *, max_sentences: int, language: str
) -> AsyncIterator[SpokenChunk]:
    stream = SentenceStream(max_sentences, language)
    async for chunk in chunks:
        stream.observe_language(chunk.language)
        for sentence in stream.take(chunk.text):
            yield SpokenChunk(text=sentence, language=chunk.language)
        if stream.check_cap():
            return
    tail = stream.flush()
    if tail is not None:
        yield SpokenChunk(text=tail, language=language)
