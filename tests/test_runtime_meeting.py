from contextlib import asynccontextmanager

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from fakeyou.browser import open_direct_page
from fakeyou.lifecycle import create_lifecycle_graph
from fakeyou.meet import enable_meeting_microphone
from fakeyou.runtime import run_meeting_session
from fakeyou.session import SessionConfig

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
async def test_worker_joins_and_leaves_real_browser_fixture_with_durable_state(tmp_path) -> None:
    pages = []

    @asynccontextmanager
    async def open_fixture(**kwargs):
        async with open_direct_page(**kwargs) as page:
            await page.route(
                "https://meet.google.com/**",
                lambda route: route.fulfill(body=MEETING_PAGE, content_type="text/html"),
            )
            pages.append(page)
            yield page

    database = tmp_path / "meeting.sqlite"
    config = SessionConfig(
        owner_id="fixture-owner",
        twin_id="fixture-twin",
        meeting_id="fixture-meeting",
        link="https://meet.google.com/abc-defg-hij",
        twin_name="Fixture Twin",
        checkpoint_path=database,
        warmup_browser=False,
        headless=True,
        session_duration_seconds=0.2,
        monitor_interval_seconds=0.1,
    )

    result = await run_meeting_session(config, open_page=open_fixture)

    assert result.phase == "ended"
    assert result.terminal_reason == "duration_limit_reached"
    assert pages[0].is_closed()
    async with AsyncSqliteSaver.from_conn_string(str(database)) as saver:
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
async def test_microphone_enable_checks_result_and_does_not_toggle_twice():
    async with open_direct_page(headless=True) as page:
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
