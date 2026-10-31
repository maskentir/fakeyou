from collections.abc import Sequence

from fakeyou.meet.meet import CallObservation
from fakeyou.perception.meet_protocol import STATUS_REMOVED, MeetUser

DRAIN_STALE_SECONDS = 5.0
_CLOSED_PEER_STATES = frozenset({"closed", "failed"})
_OUT_OF_CALL = frozenset({"removed", "ended", "left"})


class DrainClock:
    def __init__(self) -> None:
        self._seen = 0
        self._at: float | None = None

    def fresh(self, drains: int, now: float) -> bool:
        if drains != self._seen:
            self._seen = drains
            self._at = now
        return self._at is not None and now - self._at <= DRAIN_STALE_SECONDS


def protocol_call_state(own: MeetUser | None, peers: Sequence[str]) -> CallObservation | None:
    if own is None:
        return None
    connected = not peers or any(state not in _CLOSED_PEER_STATES for state in peers)
    if own.present:
        if connected:
            return CallObservation(state="in_call", detail="own device is in the meeting")
        return None
    if own.status == STATUS_REMOVED:
        return CallObservation(
            state="removed", detail=f"own device was removed (status {own.status})"
        )
    return CallObservation(
        state="ended", detail=f"own device is out of the meeting (status {own.status})"
    )


def merge_call_state(
    protocol: CallObservation | None, dom: CallObservation | None
) -> CallObservation:
    if protocol is None:
        return dom or CallObservation(state="unknown", detail="call state is not observable")
    if protocol.state == "in_call" or dom is None:
        return protocol
    return dom if dom.state in _OUT_OF_CALL else protocol
