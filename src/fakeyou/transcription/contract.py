from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fakeyou.foundation.transcript import TranscriptUpdate


class TranscriptionError(RuntimeError):
    pass


class SttConnectionLost(TranscriptionError):
    pass


TranscriptSink = Callable[[TranscriptUpdate], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SpeechSinks:
    on_update: TranscriptSink
    on_interim: TranscriptSink
    on_lost: Callable[[TranscriptionError], None]
