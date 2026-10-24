from collections.abc import AsyncIterator, Callable, Sequence
from typing import Protocol

from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.foundation.transcript import TranscriptEvent


class WholeReplier(Protocol):
    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> SpokenChunk: ...


class ReplierWholeResponder:
    def __init__(
        self,
        replier: WholeReplier,
        events: tuple[TranscriptEvent, ...],
        on_tool_start: Callable[[], None],
    ) -> None:
        self._replier = replier
        self._events = events
        self._on_tool_start = on_tool_start

    async def respond(self, _transcript: str) -> SpokenChunk:
        return await self._replier.respond(self._events, self._on_tool_start)


class SentenceSource(Protocol):
    def __call__(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
    ) -> AsyncIterator[SpokenChunk]: ...


class ReplierStreamResponder:
    def __init__(
        self,
        source: SentenceSource,
        events: tuple[TranscriptEvent, ...],
        on_tool_start: Callable[[], None],
        request_id: str,
    ) -> None:
        self._source = source
        self._events = events
        self._on_tool_start = on_tool_start
        self._request_id = request_id

    def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        return self._source(self._events, self._on_tool_start, request_id=self._request_id)
