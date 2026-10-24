from dataclasses import dataclass, field

from fakeyou.foundation.triggers import ReplyChannel

ATTENTION_WINDOW_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class AttentionOpportunity:
    trigger: str
    channel: ReplyChannel = "voice"


@dataclass(slots=True)
class AttentionPrefilter:
    max_per_window: int = 2
    window_seconds: float = ATTENTION_WINDOW_SECONDS
    _taken: list[float] = field(default_factory=list)

    def allow(self, now: float) -> bool:
        self._taken = [taken for taken in self._taken if now - taken < self.window_seconds]
        return len(self._taken) < self.max_per_window

    def take(self, now: float) -> None:
        self._taken.append(now)
