from dataclasses import dataclass
from typing import Literal

from fakeyou.perception.roster import MeetingState

PresenceSource = Literal["protocol", "dom", "unknown"]
PROTOCOL_STALE_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class PresenceReading:
    remote: int | None
    source: PresenceSource
    drift: bool


class AloneClock:
    def __init__(self, *, leave_after_seconds: float) -> None:
        self._leave_after = leave_after_seconds
        self._alone_since: float | None = None
        self._syncs_seen = 0
        self._synced_at: float | None = None
        self._drifting = False

    @property
    def drifting(self) -> bool:
        return self._drifting

    def observe(
        self,
        now: float,
        *,
        state: MeetingState | None,
        syncs: int,
        dom_remote: int | None,
    ) -> PresenceReading:
        if syncs != self._syncs_seen:
            self._syncs_seen = syncs
            self._synced_at = now
        protocol = self._fresh_protocol(state, now)
        if protocol is None:
            source: PresenceSource = "dom" if dom_remote is not None else "unknown"
            reading = PresenceReading(dom_remote, source, drift=False)
        else:
            drift = dom_remote is not None and dom_remote != protocol
            remote = dom_remote if drift and protocol == 0 else protocol
            reading = PresenceReading(remote, "protocol", drift)
        self._drifting = reading.drift
        if reading.remote is None:
            return reading
        if reading.remote > 0:
            self._alone_since = None
        elif self._alone_since is None:
            self._alone_since = now
        return reading

    def due(self, now: float) -> bool:
        return self._alone_since is not None and now - self._alone_since >= self._leave_after

    def _fresh_protocol(self, state: MeetingState | None, now: float) -> int | None:
        if state is None or not state.synced or self._synced_at is None:
            return None
        if now - self._synced_at > PROTOCOL_STALE_SECONDS:
            return None
        return state.remote_count
