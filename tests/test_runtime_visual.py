import asyncio
from pathlib import Path
from typing import Any, cast

from playwright.async_api import Page

from fakeyou.config.settings import PerceptionSettings
from fakeyou.conversation.session import SessionConfig
from fakeyou.meet.meet import DEFAULT_MEET_SIGNALS
from fakeyou.orchestration.runtime import MeetingWorker, SessionWiring, run_meeting_session
from fakeyou.visual.visual_caption import VisualCaptioner
from fakeyou.visual.visual_store import VisualStore
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import ENDED, IN_CALL, FakePageSession, meet_page
from tests._persistence_fakes import FakeSaverSession, RecordingJournal
from tests._visual_fakes import CLIP_BOX, PRESENT_SCAN, FakeShotPage, caption_model, shot_bytes


def _config(**overrides: Any) -> SessionConfig:
    values: dict[str, Any] = {
        "owner_id": "owner-1",
        "twin_id": "twin-1",
        "meeting_id": "meeting-visual",
        "link": LINK,
        "twin_name": "Twin",
    }
    values.update(overrides)
    return SessionConfig(**values)


async def test_run_meeting_session_starts_visual_observer_when_flag_on(
    tmp_path: Path,
) -> None:
    stop = asyncio.Event()
    page = meet_page(
        [IN_CALL, IN_CALL, IN_CALL, ENDED],
        on_content=lambda reads: stop.set() if reads >= 2 else None,
    )
    browser = FakePageSession(page)
    journal = RecordingJournal("meeting-visual")
    clock = FakeClock()

    result = await run_meeting_session(
        _config(visual_enabled=True),
        SessionWiring(
            stop=stop,
            open_page=browser.open,
            saver_factory=FakeSaverSession().factory,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
            visual_store=VisualStore(tmp_path / "visual"),
        ),
    )

    assert result.phase == "ended"
    assert journal.events("visual_observer_started") != []


async def test_run_meeting_session_skips_visual_observer_when_flag_off(
    tmp_path: Path,
) -> None:
    stop = asyncio.Event()
    page = meet_page(
        [IN_CALL, IN_CALL, IN_CALL, ENDED],
        on_content=lambda reads: stop.set() if reads >= 2 else None,
    )
    browser = FakePageSession(page)
    journal = RecordingJournal("meeting-visual")
    clock = FakeClock()

    result = await run_meeting_session(
        _config(),
        SessionWiring(
            stop=stop,
            open_page=browser.open,
            saver_factory=FakeSaverSession().factory,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
            visual_store=VisualStore(tmp_path / "visual"),
        ),
    )

    assert result.phase == "ended"
    assert journal.events("visual_observer_started") == []


async def test_worker_wires_captioner_and_callback_into_observer(tmp_path: Path) -> None:
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], boxes=[dict(CLIP_BOX)], shots=[shot_bytes()])
    store = VisualStore(tmp_path / "visual")
    model = caption_model("A launch timeline.")
    captioner = VisualCaptioner(model, settings=PerceptionSettings(enabled=True))
    received: list[tuple[str, str]] = []
    clock = FakeClock()
    worker = MeetingWorker(
        _config(visual_enabled=True),
        cast(Page, page),
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
        visual_store=store,
        visual_captioner=captioner,
        visual_on_caption=lambda frame, caption: received.append((frame.frame_id, caption.text)),
    )

    await worker.start_visual_observer()

    assert worker.visual_observer is not None
    assert await worker.visual_observer.poll() is None
    frame = await worker.visual_observer.poll()
    assert frame is not None
    await worker.visual_observer.drain_eager()
    assert len(model.seen) == 1
    assert received == [(frame.frame_id, "A launch timeline.")]
    await worker.close_visual_observer()
    assert worker.visual_observer is None
