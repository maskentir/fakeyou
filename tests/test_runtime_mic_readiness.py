import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from types import SimpleNamespace
from typing import Any, override

import pytest
from playwright.async_api import Error as PlaywrightError

import fakeyou.orchestration.runtime as runtime
from fakeyou.meet.meet import JoinResult, enable_meeting_microphone
from tests._audio_fakes import FakeAudio
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import (
    LEAVE_SELECTOR,
    MIC_OFF_SELECTOR,
    MIC_ON_SELECTOR,
    FakeLocator,
    FakePage,
)
from tests._persistence_fakes import RecordingJournal
from tests._runtime_support import audio_session

ROSTER_ADMISSION = JoinResult(
    "admitted", "own device was admitted to the meeting", admission="protocol"
)
DOM_ADMISSION = JoinResult("admitted", "guest admitted to the call", admission="dom")
NEVER = float("inf")
SENSITIVE = ("Harun Rasyid", "ai2@example.com", "abc-defg-hij")
TOOLBAR_PROBES = (MIC_ON_SELECTOR, MIC_OFF_SELECTOR, LEAVE_SELECTOR)
RUNTIME_RELEASE = ["audio_started", "admitted", "audio_stopped", "browser_closed"]


class TimedLocator(FakeLocator):
    def __init__(self, page: "ToolbarPage", selector: str) -> None:
        super().__init__(page, selector)
        self._toolbar = page

    def _shown(self) -> bool:
        return self._toolbar.fake_clock.now >= self._toolbar.appears.get(self.selector, NEVER)

    @override
    async def count(self) -> int:
        return 1 if self._shown() else 0

    @override
    async def is_visible(self) -> bool:
        self._toolbar.probes.append((self.selector, self._toolbar.fake_clock.now))
        return self._shown()

    @override
    async def wait_for(self, **kwargs: Any) -> None:
        if self._shown():
            return
        await self._toolbar.fake_clock.sleep(float(kwargs.get("timeout", 30000)) / 1000)
        await asyncio.sleep(0)
        if not self._shown():
            raise PlaywrightError(f"not visible yet: {self.selector}")

    @override
    async def click(self, **kwargs: Any) -> None:
        del kwargs
        self._toolbar.clicked.append(self.selector)
        if not self._shown():
            raise PlaywrightError(f"click failed: {self.selector}")
        if self.selector == MIC_OFF_SELECTOR and self._toolbar.mic_switches:
            self._toolbar.appears[MIC_ON_SELECTOR] = self._toolbar.fake_clock.now


class ToolbarPage(FakePage):
    def __init__(
        self,
        clock: FakeClock,
        appears: dict[str, float],
        *,
        mic_switches: bool = True,
        labels: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__([], [], labels=labels or {})
        self.fake_clock = clock
        self.appears = dict(appears)
        self.mic_switches = mic_switches
        self.probes: list[tuple[str, float]] = []

    @override
    def locator(self, selector: str, **kwargs: Any) -> Any:
        del kwargs
        if selector in TOOLBAR_PROBES:
            return TimedLocator(self, selector)
        return FakeLocator(self, selector)


class CountingAudio(FakeAudio):
    def __init__(self, events: list[str], clock: FakeClock, admit_delay: float = 0.0) -> None:
        super().__init__(events)
        self.clock = clock
        self.admit_delay = admit_delay
        self.stop_calls = 0

    def mark_admitted(self) -> None:
        self.clock.now += self.admit_delay

    @override
    async def stop(self) -> None:
        self.stop_calls += 1
        await super().stop()


def _toolbar(mic_off_at: float = NEVER, leave_at: float = NEVER) -> dict[str, float]:
    return {MIC_OFF_SELECTOR: mic_off_at, LEAVE_SELECTOR: leave_at}


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    prepared = audio_session(monkeypatch)
    monkeypatch.setattr(runtime, "enable_meeting_microphone", enable_meeting_microphone)

    async def no_notices(page: Any) -> tuple[str, ...]:
        del page
        return ()

    monkeypatch.setattr(runtime, "dismiss_notices", no_notices)
    return prepared


@dataclass(slots=True)
class Scenario:
    page: ToolbarPage
    admission: JoinResult = field(default_factory=lambda: ROSTER_ADMISSION)
    stop_at: float | None = None
    admit_delay: float = 0.0


@dataclass(frozen=True, slots=True)
class Outcome:
    result: Any
    journal: RecordingJournal
    audio: CountingAudio

    def events(self, name: str) -> list[dict[str, Any]]:
        return self.journal.events(name)


async def _run(
    session: SimpleNamespace,
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    monkeypatch: pytest.MonkeyPatch,
    scenario: Scenario,
) -> Outcome:
    page = scenario.page
    clock = page.fake_clock
    stop_at = scenario.stop_at

    async def join(*args: Any) -> JoinResult:
        del args
        session.events.append("admitted")
        page.fake_clock.now = 0.0
        return scenario.admission

    monkeypatch.setattr(runtime, "request_guest_join", join)

    @asynccontextmanager
    async def open_page(**kwargs: Any) -> AsyncIterator[FakePage]:
        del kwargs
        yield page
        session.events.append("browser_closed")

    sleep = clock.sleep
    if stop_at is not None:

        async def stopping_sleep(seconds: float) -> None:
            await clock.sleep(seconds)
            if clock.now >= stop_at:
                session.stop.set()
                await asyncio.sleep(0)

        sleep = stopping_sleep

    audio = CountingAudio(session.events, clock, scenario.admit_delay)
    journal = RecordingJournal()
    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(
            open_audio=partial(open_audio, audio, started=True),
            open_page=open_page,
            stop=session.stop,
            sleep=sleep,
            monotonic=clock.monotonic,
            journal=journal,
        ),
    )
    return Outcome(result, journal, audio)


async def test_t1_roster_admission_before_the_toolbar_enables_the_mic_once_it_appears(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ToolbarPage(FakeClock(), _toolbar(mic_off_at=12.0, leave_at=12.0))

    run = await _run(session, open_audio, monkeypatch, Scenario(page))

    assert run.result.terminal_reason != "audio_failure", run.result.detail
    assert MIC_OFF_SELECTOR in page.clicked
    assert [row["waited_ms"] for row in run.events("call_controls_ready")] == [12000.0]
    assert run.events("microphone_enabled")


async def test_t1b_controls_ready_at_the_last_probe_before_the_deadline_still_enable_the_mic(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ToolbarPage(FakeClock(), _toolbar(mic_off_at=18.0, leave_at=18.0))

    run = await _run(session, open_audio, monkeypatch, Scenario(page))

    assert run.result.terminal_reason != "audio_failure", run.result.detail
    assert MIC_OFF_SELECTOR in page.clicked
    assert [row["waited_ms"] for row in run.events("call_controls_ready")] == [18000.0]


async def test_t2_absent_controls_at_the_deadline_are_a_browser_failure_with_audio_stopped_once(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ToolbarPage(FakeClock(), _toolbar())

    run = await _run(session, open_audio, monkeypatch, Scenario(page))

    assert run.result.terminal_reason == "browser_failure", run.result.detail
    assert session.events.count("audio_stopped") == 1
    assert session.events == RUNTIME_RELEASE
    assert [row["outcome"] for row in run.events("call_controls_timeout")] == ["toolbar_missing"]
    assert MIC_OFF_SELECTOR not in page.clicked
    assert not run.events("microphone_enabled")


async def test_t2b_a_visible_leave_button_without_a_mic_control_is_not_mic_readiness(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ToolbarPage(FakeClock(), _toolbar(leave_at=0.0))

    run = await _run(session, open_audio, monkeypatch, Scenario(page))

    assert run.result.terminal_reason == "browser_failure", run.result.detail
    assert session.events.count("audio_stopped") == 1
    assert [row["outcome"] for row in run.events("call_controls_timeout")] == [
        "mic_control_missing"
    ]
    assert not run.events("call_controls_ready")


async def test_t3_a_visible_mic_that_cannot_be_enabled_stays_an_audio_failure(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ToolbarPage(FakeClock(), _toolbar(mic_off_at=0.0, leave_at=0.0), mic_switches=False)

    run = await _run(session, open_audio, monkeypatch, Scenario(page))

    assert run.result.terminal_reason == "audio_failure", run.result.detail
    assert session.events.count("audio_stopped") == 1
    assert "playback_stopped" not in session.events
    assert run.events("call_controls_ready")
    assert not run.events("call_controls_timeout")
    assert page.clicked.count(MIC_OFF_SELECTOR) == 1


async def test_t4_dom_admission_with_a_ready_toolbar_enables_the_mic(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ToolbarPage(FakeClock(), _toolbar(mic_off_at=0.0, leave_at=0.0))

    run = await _run(session, open_audio, monkeypatch, Scenario(page, admission=DOM_ADMISSION))

    assert run.result.terminal_reason != "audio_failure", run.result.detail
    assert MIC_OFF_SELECTOR in page.clicked
    ready = run.events("call_controls_ready")
    assert [(row["admission"], row["waited_ms"]) for row in ready] == [("dom", 0.0)]


async def test_t5_owner_stop_during_the_wait_stops_the_session_without_a_mic_click(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ToolbarPage(FakeClock(), _toolbar(mic_off_at=15.0, leave_at=15.0))

    run = await _run(session, open_audio, monkeypatch, Scenario(page, stop_at=4.0))

    assert run.result.terminal_reason == "stopped_by_owner", run.result.detail
    assert MIC_OFF_SELECTOR not in page.clicked
    assert session.events.count("audio_stopped") == 1
    assert run.events("call_controls_waiting")
    assert not run.events("call_controls_ready")
    assert not run.events("call_controls_timeout")


async def test_t5_owner_stop_after_readiness_prevents_the_mic_click(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ToolbarPage(FakeClock(), _toolbar(mic_off_at=0.0, leave_at=0.0))

    async def stopping_camera(*args: Any) -> bool:
        del args
        session.stop.set()
        return True

    monkeypatch.setattr(runtime, "disable_meeting_camera", stopping_camera)

    run = await _run(session, open_audio, monkeypatch, Scenario(page))

    assert run.result.terminal_reason == "stopped_by_owner", run.result.detail
    assert run.events("call_controls_ready")
    assert MIC_OFF_SELECTOR not in page.clicked
    assert session.events.count("audio_stopped") == 1


async def test_t6_a_controls_timeout_never_journals_sensitive_control_labels(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ToolbarPage(
        FakeClock(),
        _toolbar(),
        labels={
            "button[aria-label]": [
                "Pin Harun Rasyid's tile",
                "More options for ai2@example.com",
                "abc-defg-hij",
            ],
            "h1, h2": ["Harun Rasyid's meeting abc-defg-hij"],
        },
    )

    run = await _run(session, open_audio, monkeypatch, Scenario(page))

    written = "\n".join(run.journal.lines) + run.result.detail
    assert not [part for part in SENSITIVE if part in written]
    assert [row["heading"] for row in run.events("call_controls_timeout")] == ["unrecognised"]


async def test_t8_a_session_without_audio_activates_without_waiting_for_controls(
    session: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    page = ToolbarPage(clock, _toolbar())

    async def join(*args: Any) -> JoinResult:
        del args
        page.fake_clock.now = 0.0
        return ROSTER_ADMISSION

    monkeypatch.setattr(runtime, "request_guest_join", join)

    @asynccontextmanager
    async def open_page(**kwargs: Any) -> AsyncIterator[FakePage]:
        del kwargs
        yield page

    journal = RecordingJournal()
    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(
            open_page=open_page,
            stop=session.stop,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
        ),
    )

    assert result.terminal_reason == "stopped_by_owner", result.detail
    assert MIC_OFF_SELECTOR not in page.clicked
    assert clock.now < 1.0
    assert not journal.events("call_controls_waiting")
    assert not [selector for selector, _ in page.probes if selector != LEAVE_SELECTOR]


async def test_t9_owner_stop_at_the_deadline_wins_over_the_timeout(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ToolbarPage(FakeClock(), _toolbar(leave_at=0.0))

    run = await _run(session, open_audio, monkeypatch, Scenario(page, stop_at=20.0))

    assert run.result.terminal_reason == "stopped_by_owner", run.result.detail
    assert not run.events("call_controls_timeout")
    assert MIC_OFF_SELECTOR not in page.clicked
    assert session.events.count("audio_stopped") == 1


async def test_t11_controls_after_the_last_probe_are_never_probed_past_the_deadline(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ToolbarPage(FakeClock(), _toolbar(mic_off_at=19.5, leave_at=19.5))

    run = await _run(session, open_audio, monkeypatch, Scenario(page))

    assert run.result.terminal_reason == "browser_failure", run.result.detail
    wait_probes = [at for selector, at in page.probes if selector in TOOLBAR_PROBES]
    assert max(wait_probes) < 20.0
    assert [row["waited_ms"] for row in run.events("call_controls_timeout")] == [20000.0]
    assert MIC_OFF_SELECTOR not in page.clicked


async def test_t12_journal_separates_controls_wait_from_time_since_admission(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ToolbarPage(FakeClock(), _toolbar(mic_off_at=13.0, leave_at=13.0))

    run = await _run(session, open_audio, monkeypatch, Scenario(page, admit_delay=3.0))

    waiting = run.events("call_controls_waiting")
    ready = run.events("call_controls_ready")
    enabled = run.events("microphone_enabled")
    assert [row["admission"] for row in waiting] == ["protocol"]
    assert [(row["admission"], row["waited_ms"]) for row in ready] == [("protocol", 10000.0)]
    assert [row["since_admitted_ms"] for row in enabled] == [13000.0]
    events = [row["event"] for row in run.journal.events()]
    assert events.index("call_controls_ready") < events.index("microphone_enabled")


async def test_t12_journal_provenance_comes_from_the_typed_field_not_the_detail(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = ToolbarPage(FakeClock(), _toolbar(mic_off_at=0.0, leave_at=0.0))
    admission = JoinResult("admitted", "own device was admitted to the meeting", admission="dom")

    run = await _run(session, open_audio, monkeypatch, Scenario(page, admission=admission))

    assert [row["admission"] for row in run.events("call_controls_waiting")] == ["dom"]
    assert [row["admission"] for row in run.events("call_controls_ready")] == ["dom"]


@dataclass(frozen=True, slots=True)
class TerminalPath:
    appears: dict[str, float]
    reason: str
    mic_switches: bool = True
    stop_at: float | None = None


@pytest.mark.parametrize(
    "path",
    [
        TerminalPath(_toolbar(mic_off_at=0.0, leave_at=0.0), "stopped_by_owner"),
        TerminalPath(_toolbar(), "browser_failure"),
        TerminalPath(_toolbar(mic_off_at=0.0, leave_at=0.0), "audio_failure", mic_switches=False),
        TerminalPath(_toolbar(mic_off_at=15.0, leave_at=15.0), "stopped_by_owner", stop_at=4.0),
    ],
    ids=["mic-enabled", "controls-timeout", "mic-enable-failed", "owner-stop-during-wait"],
)
async def test_t13_prepared_audio_is_torn_down_exactly_once_on_every_terminal_path(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    path: TerminalPath,
) -> None:
    page = ToolbarPage(FakeClock(), path.appears, mic_switches=path.mic_switches)

    run = await _run(session, open_audio, monkeypatch, Scenario(page, stop_at=path.stop_at))

    assert run.result.terminal_reason == path.reason, run.result.detail
    assert session.events.count("audio_stopped") == 1
    assert run.audio.stopped
