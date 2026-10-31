import pytest

from fakeyou.meet.meet import CallObservation, CallState
from fakeyou.orchestration.call_state import merge_call_state, protocol_call_state
from fakeyou.perception.meet_protocol import MeetUser

TWIN = "devices/twin"


def own(status: int) -> MeetUser:
    return MeetUser(
        device_id=TWIN, name="Rani Twin", present=status == 1, status=status, current_user=True
    )


def observed(state: CallState) -> CallObservation:
    return CallObservation(state=state, detail=state)


@pytest.mark.parametrize(
    ("status", "peers", "expected"),
    [
        (1, ["closed", "connected"], "in_call"),
        (7, ["connected"], "removed"),
        (6, ["connected"], "ended"),
        (6, ["closed"], "ended"),
    ],
)
def test_the_own_device_status_decides_the_call_state(
    status: int, peers: list[str], expected: str
) -> None:
    observation = protocol_call_state(own(status), peers)

    assert observation is not None
    assert observation.state == expected
    assert observation.state == "in_call" or str(status) in observation.detail


@pytest.mark.parametrize("peers", [[], ["new"], ["closed", "failed"], ["connecting"]])
def test_a_present_device_without_a_connected_peer_is_left_to_the_page(peers: list[str]) -> None:
    assert protocol_call_state(own(1), peers) is None


def test_an_unknown_own_device_is_left_to_the_page() -> None:
    assert protocol_call_state(None, ["connected"]) is None


@pytest.mark.parametrize(
    ("protocol", "dom", "expected"),
    [
        (None, "removed", "removed"),
        (None, None, "unknown"),
        ("in_call", "ended", "in_call"),
        ("ended", "removed", "removed"),
        ("ended", "left", "left"),
        ("ended", "in_call", "unknown"),
        ("ended", "lobby", "ended"),
        ("removed", "unknown", "removed"),
        ("ended", None, "ended"),
    ],
)
def test_the_protocol_wins_and_the_page_only_names_the_way_out(
    protocol: CallState | None, dom: CallState | None, expected: str
) -> None:
    merged = merge_call_state(
        None if protocol is None else observed(protocol), None if dom is None else observed(dom)
    )

    assert merged.state == expected
