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
MAX_AUDIO_SECONDS = 600.0
MAX_WALL_SECONDS = 900.0
VOICE_END = "simulatorVoiceEnd"
COMPLETE_TAKE = frozenset({"speechStart", "transcript", "speechComplete", VOICE_END})

SpeechStop = Literal[
    "unexpected_clip",
    "additional_take",
    "session_limit",
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
class SpeechRecording:
    allowed: frozenset[str]
    max_audio_seconds: float = MAX_AUDIO_SECONDS
    max_wall_seconds: float = MAX_WALL_SECONDS
    clock: Callable[[], float] = time.monotonic
    sessions: int = 0
    pcm_bytes: int = 0
    opened_at: float | None = None
    closed_at: float | None = None
    upstream_close_code: int | None = None
    closing: bool = False
    recorded: list[tuple[str, int]] = field(default_factory=list)
    stop_reason: SpeechStop | None = None
    stop_detail: str = ""

    def stop(self, reason: SpeechStop, detail: str) -> SpeechStopped:
        if self.stop_reason is None:
            self.stop_reason, self.stop_detail = reason, detail
        return SpeechStopped(reason, detail)

    def _live(self) -> None:
        if self.stop_reason is not None:
            raise SpeechStopped(self.stop_reason, self.stop_detail)

    def open_session(self, handshake: Mapping[str, Any]) -> None:
        self._live()
        if self.sessions >= MAX_SESSIONS:
            raise self.stop("session_limit", f"session {self.sessions + 1} refused")
        if handshake.get("model") != MUSE_MODEL:
            raise self.stop("unexpected_handshake", f"model {handshake.get('model')!r}")
        if handshake.get("audioEncoding") != MUSE_AUDIO_ENCODING:
            raise self.stop("unsupported_audio", f"encoding {handshake.get('audioEncoding')!r}")
        self.sessions += 1
        self.opened_at = self.clock()

    @property
    def remaining_wall_seconds(self) -> float:
        opened = self.opened_at if self.opened_at is not None else self.clock()
        return max(0.0, self.max_wall_seconds - (self.clock() - opened))

    def admit_audio(self, pcm: bytes) -> None:
        self._live()
        if len(pcm) % 2:
            raise self.stop("unsupported_audio", f"{len(pcm)} bytes is not 16-bit PCM")
        if not self.remaining_wall_seconds:
            raise self.stop("deadline", f"{self.max_wall_seconds} s wall clock spent")
        if (self.pcm_bytes + len(pcm)) / PCM_BYTES_PER_SECOND > self.max_audio_seconds:
            raise self.stop("audio_limit", f"{self.max_audio_seconds} s of audio sent")
        self.pcm_bytes += len(pcm)

    def admit_claim(self, clip: str, take: int) -> None:
        self._live()
        if clip not in self.allowed:
            raise self.stop("unexpected_clip", f"{clip} is not allowed for recording")
        if take != 1 or (clip, take) in self.recorded:
            raise self.stop("additional_take", f"{clip} take {take}")
        self.recorded.append((clip, take))

    def provider_message(self, message: Mapping[str, Any]) -> None:
        if message.get("type") == "error":
            raise self.stop("provider_error", str(message.get("message", "")))

    def provider_closed(self, code: int | None, *, expected: bool) -> None:
        self.upstream_close_code = code
        self.closed_at = self.clock()
        if not expected:
            raise self.stop("provider_closed", f"upstream closed with {code}")

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
        wall = (
            None
            if self.opened_at is None
            else round((self.closed_at or self.clock()) - self.opened_at, 3)
        )
        return {
            "endpoint": MUSE_ENDPOINT,
            "model": MUSE_MODEL,
            "allowed": sorted(self.allowed),
            "sessions": self.sessions,
            "pcm_bytes": self.pcm_bytes,
            "audio_seconds": round(audio_seconds, 3),
            "wall_seconds": wall,
            "upstream_close_code": self.upstream_close_code,
            "recorded": [f"{clip} take {take}" for clip, take in self.recorded],
            "estimated_usd": round(audio_seconds / 60 * MUSE_USD_PER_AUDIO_MINUTE, 6),
            "stop_reason": self.stop_reason,
            "stop_detail": self.stop_detail,
        }
