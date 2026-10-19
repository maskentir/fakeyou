import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.observability.journal import Journal

OWN_SPEECH_HANGOVER_SECONDS = 8.0
OWN_SPEECH_MIN_WORDS = 3
REPLAY_WINDOW_FINALS = 4


def _words(text: str) -> tuple[str, ...]:
    normalized = "".join(char if char.isalnum() else " " for char in text.lower())
    return tuple(normalized.split())


def _echoed(spoken: tuple[str, ...], heard: tuple[str, ...]) -> bool:
    width = len(heard)
    if width == 0 or width > len(spoken):
        return False
    return any(spoken[start : start + width] == heard for start in range(len(spoken) - width + 1))


class OwnSpeech:
    def __init__(self, *, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._spoken: list[tuple[str, ...]] = []
        self._speaking = False
        self._until: float | None = None

    def heard_back(self, text: str) -> bool:
        if not self._speaking and (self._until is None or self._now() > self._until):
            return False
        heard = _words(text)
        if len(heard) < OWN_SPEECH_MIN_WORDS:
            return False
        return any(_echoed(spoken, heard) for spoken in self._spoken)

    @contextmanager
    def speaking(self, text: str) -> Iterator[None]:
        self._spoken.append(_words(text))
        self._speaking = True
        self._until = None
        try:
            yield
        finally:
            self._speaking = False
            self._until = self._now() + OWN_SPEECH_HANGOVER_SECONDS


class FinalGate:
    def __init__(
        self,
        own_speech: OwnSpeech,
        *,
        journal: Journal | None = None,
        tags: Mapping[str, Any] | None = None,
    ) -> None:
        self._own = own_speech
        self._journal = journal
        self._tags = dict(tags or {})
        self._history: deque[tuple[Any, ...]] = deque(maxlen=REPLAY_WINDOW_FINALS)
        self._replay: set[tuple[Any, ...]] | None = None

    @property
    def own_speech(self) -> OwnSpeech:
        return self._own

    def arm_replay(self) -> None:
        self._replay = set(self._history)

    def admit(self, update: TranscriptUpdate) -> bool:
        if not update.final:
            return True
        identity = (update.text, update.timestamp, update.voice_clusters, update.stream_id)
        replay = self._replay
        if replay is not None:
            if identity in replay:
                replay.discard(identity)
                self._drop("duplicate")
                return False
            self._replay = None
        if self._own.heard_back(update.text):
            self._drop("own_speech")
            return False
        self._history.append(identity)
        return True

    def _drop(self, reason: str) -> None:
        if self._journal is not None:
            self._journal.record("stt_final_dropped", source="stt", reason=reason, **self._tags)
