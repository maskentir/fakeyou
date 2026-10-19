from collections.abc import AsyncIterator

from langchain_core.messages import BaseMessage

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


def model_text(message: object, metadata: object, *, tool_pending: bool) -> str:
    if not isinstance(metadata, dict) or metadata.get("langgraph_node") != "model":
        return ""
    if tool_pending or not isinstance(message, BaseMessage):
        return ""
    return message.text


async def shape_sentences(
    pieces: AsyncIterator[str], *, max_sentences: int, language: str
) -> AsyncIterator[str]:
    stream = SentenceStream(max_sentences, language)
    async for piece in pieces:
        for sentence in stream.take(piece):
            yield sentence
        if stream.check_cap():
            return
    tail = stream.flush()
    if tail is not None:
        yield tail


def cap_reply(reply: str, max_sentences: int, language: str) -> str:
    if max_sentences <= 0:
        raise ValueError("max_sentences must be positive")
    if not reply.strip():
        return reply
    remaining = reply.strip()
    sentences: list[str] = []
    for _ in range(max_sentences):
        boundary = sentence_end(remaining, language=language)
        if not boundary:
            sentences.append(remaining)
            break
        sentences.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
        if not remaining:
            break
    return " ".join(sentences)
