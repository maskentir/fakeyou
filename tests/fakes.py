from __future__ import annotations

import asyncio
import json
import math
import re
import struct
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, create_autospec

import httpx2
from deepeval.models import DeepEvalBaseLLM
from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from neo4j import AsyncDriver
from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.errors import ErrorCategory

from fakeyou.conversation import ConversationResponder, TranscriptEvent
from fakeyou.journal import Journal
from fakeyou.playback import PlaybackEvent
from fakeyou.session import ConversationError, MeetingAudio, MeetingConversation, SessionResult

LINK = "https://meet.google.com/abc-defg-hij"

LOBBY = '<input placeholder="Your name"><button>Ask to join</button>'
JOIN_NOW_LOBBY = '<input placeholder="Your name"><button>Join now</button>'
DIALOG_LOBBY = (
    '<input placeholder="Your name">'
    "<div>Do you want people to see and hear you in the meeting?</div>"
    "<button>Continue without microphone and camera</button>"
)
IN_CALL = '<button aria-label="Leave call"></button><button>Mute microphone</button>'
DENIED = "<h1>You can't join this video call</h1>"
EXPLICIT_DENIED = "<h1>Someone in the call denied your request to join</h1>"
REMOVED = "<h1>You've been removed from the call</h1>"
ENDED = "<h1>The call has ended</h1>"
WAITING = LOBBY + "<div>Asking to be let in...</div>"
LEAVE_AND_WAITING = IN_CALL + "<div>Asking to be let in...</div>"
LOBBY_WITHOUT_BUTTON = '<input placeholder="Your name">'

DIALOG_SELECTOR = 'text="Continue without microphone and camera"'
NAME_SELECTOR = "placeholder:Your name"
ASK_SELECTOR = 'text="Ask to join"'
JOIN_NOW_SELECTOR = 'text="Join now"'
LEAVE_SELECTOR = 'button[aria-label="Leave call"]'
MIC_ON_SELECTOR = (
    'button[aria-label^="Turn off microphone" i],button[aria-label^="Mute microphone" i]'
)
MIC_OFF_SELECTOR = (
    'button[aria-label^="Turn on microphone" i],button[aria-label^="Unmute microphone" i]'
)
CAM_ON_SELECTOR = 'button[aria-label^="Turn off camera" i]'
CAM_OFF_SELECTOR = 'button[aria-label^="Turn on camera" i]'

NO_CAPTCHA = False
LIVE_CAPTCHA = True


class FakeKeyboard:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def type(self, text: str, delay: float = 0) -> None:
        self._page.typed.append(text)
        if text in self._page.type_failures:
            raise RuntimeError(f"typing failed: {text}")


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self._page = page
        self.selector = selector

    @property
    def first(self) -> FakeLocator:
        return self

    async def count(self) -> int:
        scripted = self._page.counts.get(self.selector, [1])
        if len(scripted) > 1:
            return scripted.pop(0)
        return scripted[0]

    async def wait_for(self, **kwargs: Any) -> None:
        if self.selector in self._page.wait_failures:
            raise RuntimeError(f"wait failed: {self.selector}")
        self._page.waited.append(self.selector)

    async def is_visible(self) -> bool:
        value = self._page.visible.get(self.selector, True)
        if isinstance(value, Exception):
            raise value
        return value

    async def click(self, **kwargs: Any) -> None:
        self._page.clicked.append(self.selector)
        remaining = self._page.click_flaky.get(self.selector, 0)
        if remaining > 0:
            self._page.click_flaky[self.selector] = remaining - 1
            raise RuntimeError(f"click failed: {self.selector}")
        if self.selector in self._page.click_failures:
            raise RuntimeError(f"click failed: {self.selector}")


class FakePage:
    def __init__(
        self,
        contents: Sequence[str | Exception] = (),
        evaluates: Sequence[Any] = (),
        counts: dict[str, list[int]] | None = None,
        click_failures: Iterable[str] = (),
        click_flaky: dict[str, int] | None = None,
        type_failures: Iterable[str] = (),
        goto_failures: int = 0,
        visible: dict[str, bool | Exception] | None = None,
        wait_failures: Iterable[str] = (),
        on_content: Callable[[int], None] | None = None,
    ) -> None:
        self._on_content = on_content
        self._reads = 0
        self._contents = list(contents)
        self._evaluates = list(evaluates)
        self.counts = {key: list(value) for key, value in (counts or {}).items()}
        self.click_failures = set(click_failures)
        self.click_flaky = dict(click_flaky or {})
        self.visible = dict(visible or {})
        self.wait_failures = set(wait_failures)
        self.type_failures = set(type_failures)
        self._goto_failures = goto_failures
        self.visited: list[str] = []
        self.init_scripts: list[str] = []
        self.clicked: list[str] = []
        self.typed: list[str] = []
        self.waited: list[str] = []
        self.evaluate_calls: list[str] = []
        self.keyboard = FakeKeyboard(self)

    async def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.visited.append(url)
        if self._goto_failures > 0:
            self._goto_failures -= 1
            raise RuntimeError("navigation timeout")
        return None

    async def content(self) -> str:
        if not self._contents:
            raise AssertionError("page content read more times than scripted")
        result = self._contents.pop(0)
        self._reads += 1
        if self._on_content is not None:
            self._on_content(self._reads)
        if isinstance(result, Exception):
            raise result
        return result

    async def evaluate(self, expression: str) -> Any:
        self.evaluate_calls.append(expression)
        if not self._evaluates:
            raise AssertionError("page evaluate called more times than scripted")
        result = self._evaluates.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def get_by_placeholder(self, text: str) -> FakeLocator:
        return FakeLocator(self, f"placeholder:{text}")


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class RecordingJournal(Journal):
    def __init__(self, meeting_id: str = "meeting-1", **kwargs: Any) -> None:
        self.lines: list[str] = []
        super().__init__(meeting_id, emit=self.lines.append, **kwargs)

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        rows = [json.loads(line) for line in self.lines]
        return [row for row in rows if name is None or row["event"] == name]


class ToolCallingFakeModel(FakeMessagesListChatModel):
    seen: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> ToolCallingFakeModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append(list(messages))
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


class OfflineJudge(DeepEvalBaseLLM):
    def load_model(self) -> OfflineJudge:
        return self

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("deterministic eval must not call a judge")

    async def a_generate(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("deterministic eval must not call a judge")

    def get_model_name(self) -> str:
        return "offline-deterministic"


def recall_call(args: dict[str, Any], *, id: str = "call-1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "recall_meeting_context", "args": args, "id": id, "type": "tool_call"}
        ],
    )


_QUERY_TOKEN = re.compile(r"[a-z0-9]+")
INVALIDATED_AT = datetime(2026, 10, 5, tzinfo=UTC)


def _query_tokens(text: str) -> set[str]:
    return {token for token in _QUERY_TOKEN.findall(text.lower()) if len(token) >= 3}


def make_graphiti(
    *,
    facts: Sequence[str] = (),
    episodes: dict[str, list[str]] | None = None,
    fact_groups: dict[str, str] | None = None,
    query_sensitive: bool = False,
    match_all: bool = False,
    invalidated: Sequence[str] = (),
    existing: Sequence[Any] = (),
    saga_episodes: dict[str, list[str]] | None = None,
    connect_error: BaseException | None = None,
    connect_gate: asyncio.Event | None = None,
    search_error: BaseException | None = None,
    search_gate: asyncio.Event | None = None,
    fail_build: bool = False,
    episode_prefix: str = "episode-",
    edges: Sequence[Any] = (),
) -> MagicMock:
    memory = create_autospec(Graphiti, instance=True)
    memory.events = []
    memory.added = []
    memory.searched = []
    memory.retrieved = []
    memory.removed = []
    memory.facts = list(facts)
    memory.fact_episodes = episodes or {}
    memory.fact_groups = fact_groups or {}
    memory.saga_episodes = list(existing)
    memory.errors = {}
    memory.gate = None
    memory.search_gate = search_gate
    memory.search_started = asyncio.Event()
    memory.search_cancelled = asyncio.Event()
    memory.driver = create_autospec(Neo4jDriver, instance=True)
    memory.driver.client = create_autospec(AsyncDriver, instance=True)

    async def build() -> None:
        memory.events.append("build")
        if fail_build:
            raise RuntimeError("index setup failed")

    async def close() -> None:
        memory.events.append("close")

    async def verify_connection() -> None:
        memory.events.append("verify")
        if connect_gate is not None:
            await connect_gate.wait()
        if connect_error is not None:
            raise connect_error

    async def search(query: str, **kwargs: Any) -> list[Any]:
        memory.searched.append({"query": query, **kwargs})
        memory.search_started.set()
        if memory.search_gate is not None:
            try:
                await memory.search_gate.wait()
            except asyncio.CancelledError:
                memory.search_cancelled.set()
                raise
        if search_error is not None:
            raise search_error
        fallback = (kwargs.get("group_ids") or ["team_alpha"])[0]
        if query_sensitive and not match_all:
            wanted = _query_tokens(query)
            selected = [fact for fact in memory.facts if wanted & _query_tokens(fact)]
        else:
            selected = list(memory.facts)
        if kwargs.get("search_filter") is not None:
            selected = [fact for fact in selected if fact not in invalidated]
        return [
            SimpleNamespace(
                uuid=f"edge-{memory.facts.index(fact)}",
                fact=fact,
                episodes=memory.fact_episodes.get(fact, []),
                group_id=memory.fact_groups.get(fact, fallback),
                invalid_at=INVALIDATED_AT if fact in invalidated else None,
                expired_at=INVALIDATED_AT if fact in invalidated else None,
            )
            for fact in selected
        ]

    async def add_episode(**kwargs: Any) -> Any:
        if memory.gate is not None:
            await memory.gate.wait()
        attempt = len(memory.added)
        if attempt in memory.errors:
            raise memory.errors.pop(attempt)
        memory.added.append(kwargs)
        uuid = (
            f"uuid-{kwargs['name']}"
            if episode_prefix == "uuid-"
            else f"{episode_prefix}{len(memory.added)}"
        )
        episode = SimpleNamespace(uuid=uuid, name=kwargs["name"])
        memory.saga_episodes.append(episode)
        return SimpleNamespace(episode=episode, edges=list(edges))

    async def retrieve_episodes(reference_time: datetime, **kwargs: Any) -> list[Any]:
        memory.retrieved.append(
            {"reference_time": reference_time, **kwargs} if saga_episodes is not None else kwargs
        )
        if saga_episodes is not None:
            remaining = [
                uuid for uuid in saga_episodes.get(kwargs["saga"], []) if uuid not in memory.removed
            ]
            return [SimpleNamespace(uuid=uuid) for uuid in remaining[: kwargs["last_n"]]]
        return list(memory.saga_episodes[-kwargs["last_n"] :])

    async def remove_episode(episode_uuid: str) -> None:
        memory.removed.append(episode_uuid)

    memory.build_indices_and_constraints.side_effect = build
    memory.close.side_effect = close
    memory.search.side_effect = search
    memory.add_episode.side_effect = add_episode
    memory.retrieve_episodes.side_effect = retrieve_episodes
    memory.remove_episode.side_effect = remove_episode
    memory.driver.client.verify_connectivity.side_effect = verify_connection
    return cast(MagicMock, memory)


class FakeAudio:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.stopped = False
        self.reconnect_requests: list[str] = []

    @property
    def is_speaking(self) -> bool:
        return False

    def request_stt_reconnect(self, reason: str) -> None:
        self.reconnect_requests.append(reason)

    def reconnect_state(self) -> str:
        return "queued" if self.reconnect_requests else "idle"

    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> None:
        self.events.append(f"speak:{text}")

    async def stop_playback(self) -> None:
        self.events.append("playback_stopped")

    async def stop(self) -> None:
        if not self.stopped:
            self.events.append("audio_stopped")
            self.stopped = True

    def raise_if_failed(self) -> None:
        pass

    async def wait_failed(self) -> None:
        await asyncio.Event().wait()


class FakeConversation:
    def __init__(self, events: list[str], fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.bound: MeetingAudio | None = None
        self.closed = False

    @property
    def is_busy(self) -> bool:
        return False

    def bind(self, session: MeetingAudio) -> None:
        self.bound = session
        self.events.append("conversation_bound")

    async def close(self) -> None:
        if not self.closed:
            self.events.append("conversation_closed")
            self.closed = True

    def raise_if_failed(self) -> None:
        if self.fail:
            raise ConversationError("conversation failed")


class FakePageSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.opened = 0
        self.closed = 0
        self.open_kwargs: list[dict[str, Any]] = []

    @asynccontextmanager
    async def open(self, **kwargs: Any) -> AsyncIterator[FakePage]:
        self.opened += 1
        self.open_kwargs.append(kwargs)
        try:
            yield self.page
        finally:
            self.closed += 1


class FakeSaverSession:
    def __init__(self) -> None:
        self.saver = InMemorySaver()

    @asynccontextmanager
    async def factory(self) -> AsyncIterator[InMemorySaver]:
        yield self.saver


class FakeSTT(FrameProcessor):
    def __init__(
        self,
        text: str = "heard",
        *,
        user_id: str = "guest",
        finalized: Sequence[bool] = (False, True),
        error: str | None = None,
        category: ErrorCategory | None = None,
        exception: Exception | None = None,
        startup: bool = False,
        signal: bool = False,
    ) -> None:
        super().__init__()
        self.text = text
        self.user_id = user_id
        self.finalized = finalized
        self.error = error
        self.category = category
        self.exception = exception
        self.startup = startup
        self.levels: list[float] = []
        self.signal = asyncio.Event()
        self.measure_signal = signal

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        triggered = (
            isinstance(frame, StartFrame) if self.startup else isinstance(frame, InputAudioRawFrame)
        )
        if triggered:
            if self.measure_signal and isinstance(frame, InputAudioRawFrame):
                assert frame.sample_rate == 16_000
                assert frame.num_channels == 1
                samples = struct.unpack(f"<{len(frame.audio) // 2}h", frame.audio)
                level = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768
                self.levels.append(level)
                if level > 0.02:
                    self.signal.set()
            if self.error is not None:
                await self.push_frame(
                    ErrorFrame(self.error, category=self.category, exception=self.exception),
                    FrameDirection.UPSTREAM,
                )
            for finalized in self.finalized:
                await self.push_frame(
                    TranscriptionFrame(self.text, self.user_id, "", finalized=finalized), direction
                )
        await self.push_frame(frame, direction)


def protocol_audio(audio: FakeAudio) -> MeetingAudio:
    return audio


def protocol_conversation(conversation: FakeConversation) -> MeetingConversation:
    return conversation


class PCMStream(httpx2.AsyncByteStream):
    def __init__(self, chunks: Sequence[bytes], error: Exception | None = None) -> None:
        self.chunks = list(chunks)
        self.error = error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error


class SessionRun(dict[str, Any]):
    def __init__(self) -> None:
        super().__init__()
        self.effect: Callable[..., Awaitable[Any]] | None = None
        self.result: dict[str, Any] = {
            "phase": "ended",
            "terminal_reason": "stopped_by_owner",
            "detail": "ok",
        }

    async def __call__(self, config: Any, **kwargs: Any) -> Any:
        self["config"] = config
        self["kwargs"] = kwargs
        self.update(kwargs)
        if self.effect is not None:
            return await self.effect(config, **kwargs)
        return SessionResult(**self.result)


class FakeReconnectableSTT(FakeSTT):
    def __init__(self) -> None:
        super().__init__(finalized=())
        self.calls = 0

    async def reconnect_when_idle(self) -> None:
        self.calls += 1


class LegacyRawReconnectSTT:
    async def _reconnect(self) -> None:
        pass


class LegacySTT:
    pass


class FakeSaga:
    def __init__(self, name: str, deleted: list[str]) -> None:
        self.name = name
        self.deleted = deleted

    async def delete(self, driver: Any) -> None:
        self.deleted.append(self.name)


class FakeWarmupPage:
    def __init__(self, fail_on: Iterable[str] = ()) -> None:
        self.visited: list[str] = []
        self.fail_on = set(fail_on)
        self.closed = False

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.visited.append(url)
        if url in self.fail_on:
            raise RuntimeError(f"warmup failed for {url}")

    async def wait_for_timeout(self, delay: float) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class FakeWarmupContext:
    def __init__(self, page: FakeWarmupPage) -> None:
        self.page = page

    async def new_page(self) -> FakeWarmupPage:
        return self.page


class FakeBrowserContext:
    def __init__(
        self,
        *,
        fail_on_init: bool = False,
        cancel_on_init: bool = False,
        fail_on_new_page: bool = False,
    ) -> None:
        self.fail_on_init = fail_on_init
        self.cancel_on_init = cancel_on_init
        self.fail_on_new_page = fail_on_new_page
        self.closed = 0

    async def add_init_script(self, script: str) -> None:
        if self.cancel_on_init:
            raise asyncio.CancelledError
        if self.fail_on_init:
            raise RuntimeError("init script failed")

    async def new_page(self) -> FakeWarmupPage:
        if self.fail_on_new_page:
            raise RuntimeError("new page failed")
        return FakeWarmupPage()

    async def close(self) -> None:
        self.closed += 1


def meet_page(contents: Sequence[str | Exception] = (), **overrides: Any) -> FakePage:
    counts = {DIALOG_SELECTOR: [0], NAME_SELECTOR: [1], ASK_SELECTOR: [1]}
    counts.update(overrides.pop("counts", {}))
    return FakePage(contents, counts=counts, **overrides)


class FakeResponder:
    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.calls: list[list[TranscriptEvent]] = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.gate = gate
        self.active = 0
        self.max_active = 0

    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> str:
        self.calls.append(list(events))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.gate is not None and len(self.calls) == 1:
                await self.gate.wait()
            return "/".join(item.text for item in events)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.active -= 1


class FakeReplyAudio(FakeAudio):
    def __init__(self) -> None:
        super().__init__([])
        self.spoken: list[str] = []
        self.indices: list[int | None] = []
        self.transcripts: list[str | None] = []
        self.stops = 0
        self.speaking = False
        self.on_playback_event: Callable[[PlaybackEvent], None] | None = None

    @property
    def is_speaking(self) -> bool:
        return self.speaking

    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> None:
        self.spoken.append(text)
        self.indices.append(sentence_index)
        self.transcripts.append(transcript_id)
        if self.on_playback_event is not None:
            self.on_playback_event(PlaybackEvent("playback_finished", text))

    async def stop_playback(self) -> None:
        self.stops += 1


def protocol_responder(responder: FakeResponder) -> ConversationResponder:
    return responder


def protocol_reply_audio(audio: FakeReplyAudio) -> MeetingAudio:
    return audio
