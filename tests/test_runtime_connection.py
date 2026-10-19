import asyncio
from typing import cast

from fakeyou.conversation.session import MeetingConversation
from fakeyou.meet.meet import DEFAULT_MEET_SIGNALS, CallObservation
from fakeyou.meet.watchdog import WatchdogCheck
from fakeyou.orchestration.runtime import MeetingWorker
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import meet_page
from tests._runtime_support import live_state, session_config


class RecordingConversation:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def pause(self) -> None:
        self.events.append("pause")

    def resume(self) -> None:
        self.events.append("resume")


def _worker() -> MeetingWorker:
    clock = FakeClock()
    return MeetingWorker(
        session_config(),
        meet_page([]),
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
    )


async def test_lost_connection_pauses_the_conversation_and_reconnect_resumes_it() -> None:
    worker = _worker()
    worker.begin(live_state("admitted"))
    conversation = RecordingConversation()
    worker.conversation = cast(MeetingConversation, conversation)

    await worker.apply_observation(
        WatchdogCheck(
            observation=CallObservation(state="lobby", detail="connection dropped"),
            events=(),
            next_check_seconds=1.0,
        )
    )
    assert worker.phase_now() == "joining"
    assert conversation.events == ["pause"]

    await worker.apply_observation(
        WatchdogCheck(
            observation=CallObservation(state="in_call", detail="back in the call"),
            events=(),
            next_check_seconds=1.0,
        )
    )
    assert conversation.events == ["pause", "resume"]
