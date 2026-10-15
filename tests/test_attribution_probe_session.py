import asyncio
from pathlib import Path

from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.session import SessionConfig
from fakeyou.foundation.attribution_probe import (
    load_probe_script,
)
from fakeyou.meet.meet import GuestJoinRequest, JoinPolling, request_guest_join
from fakeyou.orchestration.runtime import SessionWiring, run_meeting_session
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import IN_CALL, FakePageSession, meet_page
from tests._persistence_fakes import FakeSaverSession


async def test_guest_join_installs_init_scripts_before_navigation() -> None:
    page = meet_page([IN_CALL])
    clock = FakeClock()
    request = GuestJoinRequest(link=LINK, twin_name="Twin")
    result = await request_guest_join(
        page,
        request,
        polling=JoinPolling(clock.sleep, clock.monotonic),
        init_scripts=("probe();",),
    )
    assert result.status == "admitted"
    assert page.init_scripts == ["probe();"]
    assert page.visited == [f"{LINK}?hl=en"]


def test_probe_wiring_defaults_off() -> None:
    assert DEFAULTS.conversation.attribution_probe_enabled is False
    config = SessionConfig(
        owner_id=SessionId("o"),
        twin_id=SessionId("t"),
        meeting_id=SessionId("m"),
        link=LINK,
        twin_name="Twin",
    )
    assert config.attribution_probe_path is None


async def test_run_meeting_session_installs_probe_script_when_enabled(
    tmp_path: Path,
) -> None:
    stop = asyncio.Event()
    page = meet_page(
        [IN_CALL, IN_CALL, IN_CALL],
        on_content=lambda reads: stop.set() if reads >= 2 else None,
    )
    browser = FakePageSession(page)
    config = SessionConfig(
        owner_id=SessionId("o"),
        twin_id=SessionId("t"),
        meeting_id=SessionId("m"),
        link=LINK,
        twin_name="Twin",
        attribution_probe_path=tmp_path / "probe.jsonl",
    )
    result = await run_meeting_session(
        config,
        SessionWiring(
            stop=stop,
            open_page=browser.open,
            saver_factory=FakeSaverSession().factory,
            sleep=FakeClock().sleep,
            monotonic=FakeClock().monotonic,
        ),
    )
    assert result.phase == "ended"
    assert page.init_scripts == [load_probe_script()]


async def test_run_meeting_session_skips_probe_script_when_disabled() -> None:
    stop = asyncio.Event()
    page = meet_page(
        [IN_CALL, IN_CALL, IN_CALL],
        on_content=lambda reads: stop.set() if reads >= 2 else None,
    )
    browser = FakePageSession(page)
    config = SessionConfig(
        owner_id=SessionId("o"),
        twin_id=SessionId("t"),
        meeting_id=SessionId("m"),
        link=LINK,
        twin_name="Twin",
    )
    await run_meeting_session(
        config,
        SessionWiring(
            stop=stop,
            open_page=browser.open,
            saver_factory=FakeSaverSession().factory,
            sleep=FakeClock().sleep,
            monotonic=FakeClock().monotonic,
        ),
    )
    assert page.init_scripts == []
