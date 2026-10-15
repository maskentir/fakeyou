from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from playwright.async_api import Page

from fakeyou.config.identifiers import SessionId
from fakeyou.conversation.session import SessionConfig
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.meet.meet import enable_meeting_microphone
from fakeyou.orchestration.lifecycle import create_lifecycle_graph
from fakeyou.orchestration.runtime import SessionWiring, run_meeting_session
from tests._fakes_common import DATABASE_URL

MEETING_PAGE = """<!doctype html><html><body>
<input placeholder="Your name"><button id="join">Ask to join</button>
<script>
document.querySelector('#join').onclick = () => {
  document.body.innerHTML = '<button aria-label="Leave call">Leave call</button>';
  document.querySelector('button').onclick = () => {
    document.body.innerHTML = '<h1>The call has ended</h1>';
  };
};
</script></body></html>"""


@pytest.mark.browser
async def test_worker_joins_and_leaves_real_browser_fixture_with_durable_state() -> None:
    pages: list[Page] = []

    @asynccontextmanager
    async def open_fixture(**kwargs: Any) -> AsyncIterator[Page]:
        async with open_direct_page(**kwargs) as page:
            await page.route(
                "https://meet.google.com/**",
                lambda route: route.fulfill(body=MEETING_PAGE, content_type="text/html"),
            )
            pages.append(page)
            yield page

    config = SessionConfig(
        owner_id=SessionId("fixture-owner"),
        twin_id=SessionId("fixture-twin"),
        meeting_id=SessionId("fixture-meeting"),
        link="https://meet.google.com/abc-defg-hij",
        twin_name="Fixture Twin",
        database_url=DATABASE_URL,
        warmup_browser=False,
        headless=True,
        session_duration_seconds=0.2,
        monitor_interval_seconds=0.1,
    )

    result = await run_meeting_session(config, SessionWiring(open_page=open_fixture))

    assert result.phase == "ended"
    assert result.terminal_reason == "duration_limit_reached"
    assert pages[0].is_closed()
    async with open_checkpointer(DATABASE_URL) as saver:
        state = await create_lifecycle_graph(saver).aget_state(
            {"configurable": {"thread_id": "fixture-twin:fixture-meeting"}}
        )
    assert state.values["external_effects"] == ["join_requested", "leave_requested"]
    assert [item["to_phase"] for item in state.values["evidence"]] == [
        "lobby",
        "admitted",
        "leaving",
        "ended",
    ]


@pytest.mark.browser
async def test_microphone_enable_checks_result_and_does_not_toggle_twice() -> None:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.route(
            "https://meet.google.com/**",
            lambda route: route.fulfill(
                content_type="text/html",
                body="""<button aria-label="Turn on microphone (⌘+d)"
                  onclick="this.setAttribute('aria-label','Turn off microphone (⌘+d)');
                           window.clicks = (window.clicks || 0) + 1">Microphone</button>""",
            ),
        )
        await page.goto("https://meet.google.com/abc-defg-hij", wait_until="domcontentloaded")
        assert await enable_meeting_microphone(page)
        assert await enable_meeting_microphone(page)
        assert await page.evaluate("window.clicks") == 1
