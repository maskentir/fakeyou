import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

MUSE_ENDPOINT = "wss://api.meta.ai/v1/asr/realtime"
MUSE_MODEL = "muse-voice-transcribe-1.0"
MUSE_AUDIO_ENCODING = "PCM_16KHZ"
PCM_BYTES_PER_SECOND = 16_000 * 2
MUSE_USD_PER_AUDIO_MINUTE = 3.0 / 1000
MAX_SESSIONS = 1
MOST_SESSIONS = 4
MAX_AUDIO_SECONDS = 600.0
MAX_WALL_SECONDS = 900.0
HANDOVER_SECONDS = 5.0
VOICE_END = "simulatorVoiceEnd"
COMPLETE_TAKE = frozenset({"speechStart", "transcript", "speechComplete", VOICE_END})

SpeechStop = Literal[
    "unexpected_clip",
    "additional_take",
    "session_limit",
    "concurrent_session",
    "audio_limit",
    "deadline",
    "unsupported_audio",
    "unexpected_handshake",
    "provider_closed",
    "provider_error",
    "incomplete_transcript",
]


class SpeechStopped(RuntimeError):
    def __init__(self, reason: SpeechStop, detail: str) -> None:
        super().__init__(f"speech recording stopped: {reason}: {detail}")
        self.reason = reason


@dataclass(slots=True)
class SpeechSession:
    index: int
    opened_at: float
    keywords: int
    closing: bool = False
    closed_at: float | None = None
    close_code: int | None = None
    ending: str | None = None
    pcm_bytes: int = 0

    @property
    def open(self) -> bool:
        return self.closed_at is None

    def report(self, origin: float) -> dict[str, object]:
        return {
            "index": self.index,
            "opened_s": round(self.opened_at - origin, 3),
            "closed_s": None if self.closed_at is None else round(self.closed_at - origin, 3),
            "close_code": self.close_code,
            "ending": self.ending,
            "keywords": self.keywords,
            "pcm_bytes": self.pcm_bytes,
        }


@dataclass(slots=True)
class SpeechRecording:
    allowed: frozenset[str]
    max_sessions: int = MAX_SESSIONS
    max_audio_seconds: float = MAX_AUDIO_SECONDS
    max_wall_seconds: float = MAX_WALL_SECONDS
    handover_seconds: float = HANDOVER_SECONDS
    clock: Callable[[], float] = time.monotonic
    sessions: list[SpeechSession] = field(default_factory=list)
    recorded: list[tuple[str, int]] = field(default_factory=list)
    stop_reason: SpeechStop | None = None
    stop_detail: str = ""
    _idle: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        if not 1 <= self.max_sessions <= MOST_SESSIONS:
            raise ValueError(f"speech recording allows 1 to {MOST_SESSIONS} sessions")
        self._idle.set()

    def stop(self, reason: SpeechStop, detail: str) -> SpeechStopped:
        if self.stop_reason is None:
            self.stop_reason, self.stop_detail = reason, detail
        current = self.current
        if current is not None:
            self._close(current, current.close_code, reason)
        return SpeechStopped(reason, detail)

    def _live(self) -> None:
        if self.stop_reason is not None:
            raise SpeechStopped(self.stop_reason, self.stop_detail)

    @property
    def current(self) -> SpeechSession | None:
        return self.sessions[-1] if self.sessions and self.sessions[-1].open else None

    @property
    def first_opened_at(self) -> float | None:
        return self.sessions[0].opened_at if self.sessions else None

    @property
    def pcm_bytes(self) -> int:
        return sum(session.pcm_bytes for session in self.sessions)

    async def open_session(self, handshake: Mapping[str, Any]) -> SpeechSession:
        self._live()
        current = self.current
        if current is not None and not current.closing:
            raise self.stop("concurrent_session", f"session {current.index} is still open")
        if current is not None:
            try:
                await asyncio.wait_for(self._idle.wait(), self.handover_seconds)
            except TimeoutError as error:
                raise self.stop(
                    "concurrent_session", f"session {current.index} did not close"
                ) from error
            self._live()
        if len(self.sessions) >= self.max_sessions:
            raise self.stop("session_limit", f"session {len(self.sessions) + 1} refused")
        if handshake.get("model") != MUSE_MODEL:
            raise self.stop("unexpected_handshake", f"model {handshake.get('model')!r}")
        if handshake.get("audioEncoding") != MUSE_AUDIO_ENCODING:
            raise self.stop("unsupported_audio", f"encoding {handshake.get('audioEncoding')!r}")
        if self.first_opened_at is not None and not self.remaining_wall_seconds:
            raise self.stop("deadline", f"{self.max_wall_seconds} s wall clock spent")
        session = SpeechSession(
            len(self.sessions) + 1, self.clock(), len(handshake.get("keywords") or ())
        )
        self.sessions.append(session)
        self._idle.clear()
        return session

    @property
    def remaining_wall_seconds(self) -> float:
        origin = self.first_opened_at
        if origin is None:
            return self.max_wall_seconds
        return max(0.0, self.max_wall_seconds - (self.clock() - origin))

    def admit_audio(self, pcm: bytes) -> None:
        self._live()
        current = self.current
        if current is None:
            raise self.stop("concurrent_session", "audio arrived outside a session")
        if len(pcm) % 2:
            raise self.stop("unsupported_audio", f"{len(pcm)} bytes is not 16-bit PCM")
        if not self.remaining_wall_seconds:
            raise self.stop("deadline", f"{self.max_wall_seconds} s wall clock spent")
        if (self.pcm_bytes + len(pcm)) / PCM_BYTES_PER_SECOND > self.max_audio_seconds:
            raise self.stop("audio_limit", f"{self.max_audio_seconds} s of audio sent")
        current.pcm_bytes += len(pcm)

    def admit_claim(self, clip: str, take: int) -> None:
        self._live()
        if clip not in self.allowed:
            raise self.stop("unexpected_clip", f"{clip} is not allowed for recording")
        if take != 1 or (clip, take) in self.recorded:
            raise self.stop("additional_take", f"{clip} take {take}")
        self.recorded.append((clip, take))

    def worker_closing(self) -> None:
        current = self.current
        if current is not None:
            current.closing = True

    def provider_message(self, message: Mapping[str, Any]) -> None:
        if message.get("type") == "error":
            raise self.stop("provider_error", str(message.get("message", "")))

    def _close(self, session: SpeechSession, code: int | None, ending: str) -> None:
        if session.open:
            session.closed_at = self.clock()
            session.close_code = code
            session.ending = ending
        self._idle.set()

    def provider_closed(self, code: int | None, *, expected: bool) -> None:
        current = self.current
        if current is None:
            return
        if not expected:
            current.close_code = code
            raise self.stop("provider_closed", f"session {current.index} closed with {code}")
        self._close(current, code, "clean")

    def verify(self, messages: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]]) -> None:
        self._live()
        for clip in sorted(self.allowed):
            if (clip, 1) not in self.recorded:
                raise self.stop("incomplete_transcript", f"{clip} was never heard")
        for key in self.recorded:
            kinds = {str(message.get("type")) for message in messages.get(key, ())}
            completed = [
                message
                for message in messages.get(key, ())
                if message.get("type") == "speechComplete"
                and str(message.get("transcript") or "").strip()
            ]
            if not COMPLETE_TAKE <= kinds or not completed:
                missing = sorted(COMPLETE_TAKE - kinds) or ["speechComplete transcript"]
                raise self.stop("incomplete_transcript", f"{key[0]} take {key[1]}: {missing}")

    def report(self) -> dict[str, object]:
        audio_seconds = self.pcm_bytes / PCM_BYTES_PER_SECOND
        origin = self.first_opened_at
        closed = [session.closed_at for session in self.sessions if session.closed_at is not None]
        wall = None
        if origin is not None:
            end = self.clock() if self.current is not None or not closed else max(closed)
            wall = round(end - origin, 3)
        return {
            "endpoint": MUSE_ENDPOINT,
            "model": MUSE_MODEL,
            "allowed": sorted(self.allowed),
            "max_sessions": self.max_sessions,
            "sessions": len(self.sessions),
            "session_lifecycles": [
                session.report(origin if origin is not None else 0.0) for session in self.sessions
            ],
            "pcm_bytes": self.pcm_bytes,
            "audio_seconds": round(audio_seconds, 3),
            "wall_seconds": wall,
            "recorded": [f"{clip} take {take}" for clip, take in self.recorded],
            "estimated_usd": round(audio_seconds / 60 * MUSE_USD_PER_AUDIO_MINUTE, 6),
            "stop_reason": self.stop_reason,
            "stop_detail": self.stop_detail,
        }
