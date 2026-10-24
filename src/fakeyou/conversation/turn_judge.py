import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, ConfigDict, Field

from fakeyou.conversation.meeting_messages import MeetingHistory, spoken_by
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.persistence.record import TranscriptEntry, TwinSpeechEntry

JudgeAction = Literal["reply", "clarify", "stay_silent"]
Addressee = Literal["twin", "someone_else", "everyone", "unclear"]
SPEAKING_NOW = "speaking now"

_JUDGE_RULES = (
    "You read a live meeting for a digital twin and decide what the latest line means for it. "
    "The twin answers to the names listed in the input. Lines come from live speech "
    "recognition: words can be misheard and the latest line can still be growing, so read "
    "each line by its most plausible meaning in this conversation.\n"
    "heard: restate the latest line as the speaker most likely meant it, in the speaker's "
    "language.\n"
    "finished: true when the speaker has completed the turn: a finished question, statement, "
    "answer, request or closing. False when the line trails off, ends on a connective or a "
    "filler, announces more to come, or only asks the others to wait while the speaker "
    "keeps the floor.\n"
    "backchannel: true when the line only acknowledges or encourages whoever holds the floor "
    "and does not take a turn of its own.\n"
    "addressee: twin when the line is meant for the twin, by name or as a reply to what the "
    "twin just said or asked; someone_else when it is meant for another person; everyone when "
    "it is put to the whole room; unclear otherwise.\n"
    "action: reply when the twin should answer now; clarify when the line is meant for the "
    "twin but too unclear to answer; stay_silent otherwise, including talk between other "
    "people, backchannels, a line that is not finished, and a question to everyone that does "
    "not ask for something the twin can answer for its owner.\n"
    "confidence: how sure you are about action, from 0 to 1."
)


class TurnJudgement(BaseModel):
    model_config = ConfigDict(frozen=True)

    heard: str = Field(description="The latest line as the speaker most likely meant it.")
    finished: bool = Field(description="The speaker has completed the turn.")
    backchannel: bool = Field(description="The line only acknowledges whoever holds the floor.")
    addressee: Addressee
    action: JudgeAction
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class JudgeLine:
    speaker: str
    text: str
    twin: bool = False


@dataclass(frozen=True, slots=True)
class JudgeWindow:
    earlier: tuple[JudgeLine, ...]
    latest: JudgeLine
    twin_speaking: bool = False

    @property
    def key(self) -> tuple[object, ...]:
        return (self.earlier, _normalized(self.latest.text), self.twin_speaking)


class TurnJudge(Protocol):
    async def judge(self, window: JudgeWindow) -> TurnJudgement: ...


def _normalized(text: str) -> str:
    return " ".join("".join(ch for ch in text.casefold() if ch.isalnum() or ch.isspace()).split())


def _line(entry: TranscriptEntry | TwinSpeechEntry) -> JudgeLine:
    if isinstance(entry, TwinSpeechEntry):
        return JudgeLine(speaker="twin", text=entry.text, twin=True)
    return JudgeLine(speaker=spoken_by(entry), text=entry.text)


def _spoken_lines(history: MeetingHistory, *, skip: str | None) -> list[JudgeLine]:
    return [
        _line(entry)
        for entry in history.entries()
        if isinstance(entry, TwinSpeechEntry)
        or (
            isinstance(entry, TranscriptEntry)
            and entry.source == "meeting_audio"
            and entry.id != skip
        )
    ]


@dataclass(frozen=True, slots=True)
class WindowSource:
    history: MeetingHistory
    window_lines: int

    def for_final(self, event: TranscriptEvent, *, twin_speaking: bool) -> JudgeWindow:
        earlier = _spoken_lines(self.history, skip=event.id)[-self.window_lines :]
        latest = JudgeLine(speaker=spoken_by(event), text=event.text)
        return JudgeWindow(tuple(earlier), latest, twin_speaking)

    def for_interim(self, text: str, *, twin_speaking: bool) -> JudgeWindow:
        earlier = _spoken_lines(self.history, skip=None)[-self.window_lines :]
        return JudgeWindow(tuple(earlier), JudgeLine(SPEAKING_NOW, text), twin_speaking)


def _rendered(window: JudgeWindow, twin_names: Sequence[str]) -> str:
    return json.dumps(
        {
            "twin_names": list(twin_names),
            "twin_is_speaking": window.twin_speaking,
            "earlier": [
                {"speaker": line.speaker, "text": line.text, "by_twin": line.twin}
                for line in window.earlier
            ],
            "latest": {"speaker": window.latest.speaker, "text": window.latest.text},
        },
        ensure_ascii=False,
    )


class ModelTurnJudge:
    def __init__(
        self, model: BaseChatModel, *, twin_names: Sequence[str], timeout_seconds: float
    ) -> None:
        self._structured: Runnable[Sequence[BaseMessage], object] = model.with_structured_output(
            TurnJudgement
        )
        self._twin_names = tuple(twin_names)
        self._timeout_seconds = timeout_seconds

    async def judge(self, window: JudgeWindow) -> TurnJudgement:
        messages: list[BaseMessage] = [
            SystemMessage(content=_JUDGE_RULES),
            HumanMessage(content=_rendered(window, self._twin_names)),
        ]
        result = await asyncio.wait_for(
            self._structured.ainvoke(messages), timeout=self._timeout_seconds
        )
        return TurnJudgement.model_validate(result)


class JudgeCache:
    def __init__(self, judge: TurnJudge) -> None:
        self._judge = judge
        self._key: tuple[object, ...] | None = None
        self._task: asyncio.Task[TurnJudgement] | None = None

    def prefetch(self, window: JudgeWindow) -> bool:
        if self._key == window.key and self._task is not None:
            return False
        self.cancel()
        self._key = window.key
        self._task = asyncio.create_task(self._judge.judge(window))
        self._task.add_done_callback(_consume_failure)
        return True

    async def judgement(self, window: JudgeWindow) -> TurnJudgement:
        if self._key != window.key or self._task is None:
            self.prefetch(window)
        task = self._task
        if task is None:
            raise RuntimeError("the judge task starts before it is awaited")
        return await asyncio.shield(task)

    def cancel(self) -> None:
        task, self._task, self._key = self._task, None, None
        if task is not None and not task.done():
            task.cancel()

    async def wait_idle(self) -> None:
        task = self._task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


def _consume_failure(task: asyncio.Task[TurnJudgement]) -> None:
    if not task.cancelled():
        task.exception()
