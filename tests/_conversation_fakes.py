from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, override

from deepeval.models import DeepEvalBaseLLM
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.session import ConversationError, MeetingAudio
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.foundation.triggers import ReplyChannel


class ToolCallingFakeModel(FakeMessagesListChatModel):
    seen: list[list[BaseMessage]] = []

    @override
    def bind_tools(self, tools: Any, **kwargs: Any) -> ToolCallingFakeModel:
        return self

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    @override
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
    @override
    def load_model(self) -> OfflineJudge:
        return self

    @override
    def generate(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("deterministic eval must not call a judge")

    @override
    async def a_generate(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("deterministic eval must not call a judge")

    @override
    def get_model_name(self) -> str:
        return "offline-deterministic"


def recall_call(args: dict[str, Any], *, id: str = "call-1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "recall_meeting_context", "args": args, "id": id, "type": "tool_call"}
        ],
    )


class FakeConversation:
    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.bound: MeetingAudio | None = None
        self.closed = False
        self.chat: list[str] = []
        self.attention: list[str] = []

    @property
    def is_busy(self) -> bool:
        return False

    def receive_chat(self, event: TranscriptEvent) -> bool:
        self.chat.append(event.text)
        return True

    def receive_attention(self, event: TranscriptEvent, *, channel: ReplyChannel) -> bool:
        del event
        self.attention.append(channel)
        return True

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

    async def pause(self) -> None:
        self.events.append("conversation_paused")

    def resume(self) -> None:
        self.events.append("conversation_resumed")


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
    ) -> SpokenChunk:
        del on_tool_start
        self.calls.append(list(events))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.gate is not None and len(self.calls) == 1:
                await self.gate.wait()
            return SpokenChunk(
                text="/".join(item.text for item in events), language=DEFAULTS.speech.language
            )
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.active -= 1
