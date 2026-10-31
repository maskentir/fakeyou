from collections.abc import Sequence

from fakeyou.meet.meet import CallObservation
from fakeyou.perception.meet_protocol import STATUS_REMOVED, MeetUser

DRAIN_STALE_SECONDS = 5.0
_OUT_OF_CALL = frozenset({"removed", "ended", "left"})


def protocol_call_state(own: MeetUser | None, peers: Sequence[str]) -> CallObservation | None:
    if own is None:
        return None
    if own.present:
        if "connected" in peers:
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
    if dom.state in _OUT_OF_CALL:
        return dom
    if dom.state == "in_call":
        return CallObservation(
            state="unknown", detail=f"page still shows the call: {protocol.detail}"
        )
    return protocol
