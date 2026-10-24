import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.session import ConversationError
from fakeyou.conversation.turn_graph import build_turn_graph
from fakeyou.conversation.turn_lifecycle import TurnLifecycle
from tests._audio_fakes import wait_until


class FakeSession:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.playback_stops = 0
        self.is_speaking = False

    async def speak(self, text: str, **_: Any) -> None:
        self.spoken.append(text)

    async def stop_playback(self) -> None:
        self.playback_stops += 1

    async def stop(self) -> None:
        return None

    def raise_if_failed(self) -> None:
        return None

    async def wait_failed(self) -> None:
        await asyncio.Event().wait()


class FakeResponder:
    async def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        yield SpokenChunk(text="Yes, I am here.", language=DEFAULTS.speech.language)


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, event: str, **_: Any) -> None:
        self.events.append(event)


def _lifecycle() -> tuple[TurnLifecycle, FakeJournal]:
    journal = FakeJournal()
    graph = build_turn_graph(decider=None, window_turns=1)
    return TurnLifecycle(graph, journal=journal, responder=FakeResponder()), journal


async def test_bind_attaches_the_graph_and_records_the_runtime() -> None:
    lifecycle, journal = _lifecycle()
    session = FakeSession()

    lifecycle.bind(session)

    assert lifecycle.session is session
    assert lifecycle.runtime is not None
    assert lifecycle.runtime.journal is journal
    assert lifecycle.runtime.floor is lifecycle.floor
    await lifecycle.runtime.speaker.speak("hello")
    assert session.spoken == ["hello"]


async def test_bind_rejects_a_second_session_and_a_closed_lifecycle() -> None:
    lifecycle, _ = _lifecycle()
    lifecycle.bind(FakeSession())

    with pytest.raises(ConversationError, match="already owns an audio session"):
        lifecycle.bind(FakeSession())

    await lifecycle.close()
    with pytest.raises(ConversationError, match="is closed"):
        lifecycle.bind(FakeSession())


def test_raise_if_failed_is_silent_on_a_clean_lifecycle() -> None:
    lifecycle, _ = _lifecycle()
    lifecycle.bind(FakeSession())

    lifecycle.raise_if_failed()


def test_raise_if_failed_raises_the_recorded_failure() -> None:
    lifecycle, _ = _lifecycle()
    cause = RuntimeError("speech collapsed")

    lifecycle.fail(cause)

    with pytest.raises(ConversationError, match="meeting conversation failed") as raised:
        lifecycle.raise_if_failed()
    assert raised.value.__cause__ is cause


async def test_pause_stops_ingress_without_ending_the_session() -> None:
    lifecycle, _ = _lifecycle()
    session = FakeSession()
    lifecycle.bind(session)
    session.is_speaking = True

    await lifecycle.pause()

    assert session.playback_stops == 1
    assert lifecycle.is_paused is True
    assert lifecycle.accepts_ingress is False
    assert lifecycle.session is session

    lifecycle.resume()

    assert lifecycle.accepts_ingress is True


async def test_close_ends_the_session_and_releases_the_speech_floor() -> None:
    lifecycle, _ = _lifecycle()
    lifecycle.bind(FakeSession())
    generation = lifecycle.floor.generation
    held = asyncio.create_task(lifecycle.floor.hold(wait_until(asyncio.Event())))
    await asyncio.sleep(0)

    await lifecycle.close()

    assert await held is False
    assert lifecycle.is_closed is True
    assert lifecycle.session is None
    assert lifecycle.runtime is None
    assert lifecycle.accepts_ingress is False
    assert lifecycle.floor.generation > generation
    assert lifecycle.floor.admits(generation) is False
