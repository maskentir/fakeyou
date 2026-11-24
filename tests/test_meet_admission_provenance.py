from typing import Any

from fakeyou.meet.meet import GuestJoinRequest, JoinPolling, request_guest_join
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import IN_CALL, LOBBY, NO_CAPTCHA, meet_page


def _request(**overrides: Any) -> GuestJoinRequest:
    return GuestJoinRequest(**{"link": LINK, "twin_name": "Twin", **overrides})


async def test_roster_admission_is_typed_protocol() -> None:
    clock = FakeClock()
    page = meet_page([LOBBY, LOBBY], evaluates=[NO_CAPTCHA] * 8)

    result = await request_guest_join(
        page, _request(), polling=JoinPolling(clock.sleep, clock.monotonic, None, lambda: True)
    )

    assert result.status == "admitted"
    assert result.admission == "protocol"


async def test_dom_admission_is_typed_dom() -> None:
    clock = FakeClock()

    result = await request_guest_join(
        meet_page([IN_CALL], evaluates=[NO_CAPTCHA] * 8),
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert result.admission == "dom"


async def test_admission_found_by_the_lobby_timeout_recheck_is_typed_dom() -> None:
    clock = FakeClock()
    page = meet_page([LOBBY] * 4 + [IN_CALL], evaluates=[NO_CAPTCHA] * 8)

    result = await request_guest_join(
        page,
        _request(lobby_timeout_seconds=5.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert result.admission == "dom"


async def test_a_non_admission_carries_no_provenance() -> None:
    clock = FakeClock()
    page = meet_page([LOBBY] * 5, evaluates=[NO_CAPTCHA] * 8)

    result = await request_guest_join(
        page,
        _request(lobby_timeout_seconds=5.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "timeout"
    assert result.admission is None
