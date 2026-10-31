import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, ConfigDict, Field

from fakeyou.conversation.meeting_messages import MeetingHistory, spoken_by
from fakeyou.foundation.participation import mentions_whole_phrase
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.persistence.record import TranscriptEntry, TwinSpeechEntry
from fakeyou.session.identity import PresenceFact

JudgeAction = Literal["reply", "clarify", "stay_silent"]
MeetAction = Literal["none", "raise_hand", "lower_hand", "react", "mute", "unmute", "leave"]
EvidenceNeed = Literal["screen", "participant", "chat_history", "memory"]
StateQuestionField = Literal[
    "none", "attendance", "participant_count", "presenter", "screen_title", "own_action_status"
]
ReactionEmoji = Literal[
    "\U0001f496",
    "\U0001f44d",
    "\U0001f44e",
    "\U0001f389",
    "\U0001f44f",
    "\U0001f602",
    "\U0001f62e",
    "\U0001f622",
    "\U0001f914",
]
Addressee = Literal["twin", "someone_else", "everyone", "unclear"]
SPEAKING_NOW = "speaking now"
JUDGE_CONCURRENCY = 4

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
    "logistics_check: true when the line only checks meeting readiness or connection, "
    "such as whether a screen is visible, a voice is audible, or the meeting can start.\n"
    "addressee: twin when the line is meant for the twin: it names the twin, answers a "
    "question or request the twin itself just put to that speaker, or directly continues an "
    "exchange with the twin, or it is an imperative aimed at whoever is currently "
    "speaking (stop, wait, say that again) while twin_is_speaking is true. The twin "
    "having recently spoken, or spoken about the same "
    "topic, is not by itself addressing. someone_else when it is meant for another person; "
    "everyone when it is put to the whole room, including room checks such as whether a "
    "screen is visible, a voice audible, or the meeting should start. A room check is "
    "everyone even immediately after the twin spoke, unless sole_human_responder is "
    "true, in which case that sole participant's logistics check is for the twin; "
    "unclear otherwise.\n"
    "action: reply when the twin should answer now; clarify when the line is meant for the "
    "twin but too unclear to answer; stay_silent otherwise, including talk between other "
    "people, backchannels, a line that is not finished, and any question put to everyone "
    "while sole_human_responder is false, even one the twin could answer. When "
    "sole_human_responder is true, a logistics check from that participant is for "
    "the twin. participants are display names each endpoint claims, not verified "
    "identities; presence says how certain the attendance is, and an incomplete or "
    "stale presence means more people may be listening.\n"
    "meet_action: when the speaker asks the twin itself to perform one meeting action "
    "(raise or lower its hand, send a reaction, mute or unmute itself, leave the call), "
    "name it; otherwise none. Talking about an action, or asking someone else to do it, "
    "is none.\n"
    "action_only: true when a requested meeting action or chat send is the line's only "
    "request and nothing else needs a spoken answer.\n"
    "requests_chat: true when the speaker asks the twin to write or send something "
    "in the meeting chat. Merely mentioning chat, or a chat message existing, is not "
    "a request.\n"
    "evidence: only the evidence the latest line needs beyond the conversation already "
    "shown. Use screen when the answer depends on what is being shared or shown; "
    "participant only when it depends on a specific participant's camera or appearance, "
    "not who is attending or how many people are present; chat_history "
    "when it asks about earlier chat; memory when it asks about the owner's earlier "
    "facts, preferences or meetings. Leave it empty for greetings, action requests, state "
    "questions, and answers available from the conversation.\n"
    "state_question: when the latest line asks only about meeting state the system can read "
    "directly, name it: attendance for who is here, participant_count for how many people "
    "are here, presenter for who is presenting, screen_title for what is being shared by "
    "name, own_action_status for whether the twin itself did, has done or is doing one of its "
    "own meeting actions, such as whether it raised or lowered its hand, reacted, muted or "
    "unmuted, or sent a chat message, including questions like 'did you raise your hand?'; "
    "otherwise none. A question about what shared content says or means is not a state "
    "question; use evidence screen for it.\n"
    "language: the language of the latest line, id for Indonesian words and en for English "
    "words.\n"
    "reaction_emoji: when meet_action is react, the closest of Meet's reaction glyphs; "
    "otherwise null.\n"
    "confidence: how sure you are about action, from 0 to 1."
)


class TurnJudgement(BaseModel):
    model_config = ConfigDict(frozen=True)

    heard: str = Field(description="The latest line as the speaker most likely meant it.")
    finished: bool = Field(description="The speaker has completed the turn.")
    backchannel: bool = Field(description="The line only acknowledges whoever holds the floor.")
    logistics_check: bool = Field(
        default=False, description="The line only checks meeting readiness or connection."
    )
    addressee: Addressee
    action: JudgeAction
    confidence: float = Field(ge=0.0, le=1.0)
    requests_chat: bool = Field(
        default=False, description="The speaker asks the twin to write in the meeting chat."
    )
    meet_action: MeetAction = Field(
        default="none",
        description="A meeting action the speaker asks the twin itself to perform.",
    )
    action_only: bool = Field(
        default=False,
        description="Whether the requested action is the line's only request.",
    )
    evidence: tuple[EvidenceNeed, ...] = Field(
        default=(),
        description="Evidence required before the twin can answer the latest line.",
    )
    language: Literal["id", "en"] | None = Field(
        default=None, description="The language of the latest line."
    )
    state_question: StateQuestionField = Field(
        default="none",
        description="Meeting state the latest line asks about, answered without the model.",
    )
    reaction_emoji: ReactionEmoji | None = Field(
        default=None,
        description="The Meet reaction glyph when the requested action is react.",
    )


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
    names_twin: bool = False
    presence: PresenceFact | None = None
    transcript_id: str | None = None

    @property
    def sole_human_responder(self) -> bool:
        return self.presence is not None and self.presence.sole_human_responder

    @property
    def key(self) -> tuple[object, ...]:
        return (
            self.earlier,
            _normalized(self.latest.text),
            self.twin_speaking,
            self.names_twin,
            self.presence,
        )


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
    twin_names: tuple[str, ...] = ()
    presence: Callable[[], PresenceFact] | None = None

    def _names_twin(self, text: str) -> bool:
        return any(mentions_whole_phrase(text, name) for name in self.twin_names)

    def _presence(self) -> PresenceFact | None:
        return self.presence() if self.presence is not None else None

    def for_final(self, event: TranscriptEvent, *, twin_speaking: bool) -> JudgeWindow:
        earlier = _spoken_lines(self.history, skip=event.id)[-self.window_lines :]
        latest = JudgeLine(speaker=spoken_by(event), text=event.text)
        return JudgeWindow(
            tuple(earlier),
            latest,
            twin_speaking,
            names_twin=self._names_twin(event.text),
            presence=self._presence(),
            transcript_id=event.id,
        )

    def for_interim(self, text: str, *, twin_speaking: bool) -> JudgeWindow:
        earlier = _spoken_lines(self.history, skip=None)[-self.window_lines :]
        return JudgeWindow(
            tuple(earlier),
            JudgeLine(SPEAKING_NOW, text),
            twin_speaking,
            names_twin=self._names_twin(text),
            presence=self._presence(),
        )


def _human_speakers(window: JudgeWindow) -> list[str]:
    speakers = {
        line.speaker for line in window.earlier if not line.twin and line.speaker != SPEAKING_NOW
    }
    latest = window.latest
    if not latest.twin and latest.speaker != SPEAKING_NOW:
        speakers.add(latest.speaker)
    return sorted(speakers)


def _presence_payload(presence: PresenceFact | None) -> dict[str, object]:
    if presence is None:
        return {"status": "unknown", "confirmed_min": 0, "plausible_max": None, "unresolved": 0}
    return {
        "status": presence.status,
        "confirmed_min": presence.confirmed_min,
        "plausible_max": presence.plausible_max,
        "unresolved": presence.unresolved,
    }


def _participants_payload(presence: PresenceFact | None) -> list[dict[str, str]]:
    if presence is None:
        return []
    return [
        {"ref": f"p{index}", "shown_as": participant.display_claim, "name_is": "claimed"}
        for index, participant in enumerate(presence.participants, start=1)
    ]


def _rendered(window: JudgeWindow, twin_names: Sequence[str]) -> str:
    humans = _human_speakers(window)
    return json.dumps(
        {
            "twin_names": list(twin_names),
            "twin_is_speaking": window.twin_speaking,
            "human_speakers": humans,
            "participants": _participants_payload(window.presence),
            "presence": _presence_payload(window.presence),
            "sole_human_responder": window.sole_human_responder,
            "earlier": [
                {"speaker": line.speaker, "text": line.text, "by_twin": line.twin}
                for line in window.earlier
            ],
            "latest": {"speaker": window.latest.speaker, "text": window.latest.text},
            "latest_names_twin": window.names_twin,
        },
        ensure_ascii=False,
    )


class ModelTurnJudge:
    def __init__(
        self,
        model: BaseChatModel,
        *,
        twin_names: Sequence[str],
        timeout_seconds: float,
        concurrency: int = JUDGE_CONCURRENCY,
    ) -> None:
        self._structured: Runnable[Sequence[BaseMessage], object] = model.with_structured_output(
            TurnJudgement
        )
        self._twin_names = tuple(twin_names)
        self._timeout_seconds = timeout_seconds
        self._slots = asyncio.Semaphore(max(1, concurrency))

    async def judge(self, window: JudgeWindow) -> TurnJudgement:
        messages: list[BaseMessage] = [
            SystemMessage(content=_JUDGE_RULES),
            HumanMessage(content=_rendered(window, self._twin_names)),
        ]
        async with self._slots:
            result = await asyncio.wait_for(
                self._structured.ainvoke(messages), timeout=self._timeout_seconds
            )
        return TurnJudgement.model_validate(result)


class JudgeCache:
    def __init__(self, judge: TurnJudge, *, keep: int = 16) -> None:
        self._judge = judge
        self._keep = max(1, keep)
        self._tasks: dict[tuple[object, ...], asyncio.Task[TurnJudgement]] = {}

    def prefetch(self, window: JudgeWindow) -> bool:
        key = window.key
        if key in self._tasks:
            return False
        task = asyncio.create_task(self._judge.judge(window))
        self._tasks[key] = task
        task.add_done_callback(_consume_failure)
        self._prune()
        return True

    async def judgement(self, window: JudgeWindow) -> TurnJudgement:
        key = window.key
        task = self._tasks.get(key)
        if task is None:
            self.prefetch(window)
            task = self._tasks.get(key)
        if task is None:
            raise RuntimeError("the judge task starts before it is awaited")
        return await asyncio.shield(task)

    def cancel(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()

    async def wait_idle(self) -> None:
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _prune(self) -> None:
        if len(self._tasks) <= self._keep:
            return
        for key, task in list(self._tasks.items()):
            if len(self._tasks) <= self._keep:
                break
            if task.done():
                del self._tasks[key]


def _consume_failure(task: asyncio.Task[TurnJudgement]) -> None:
    if not task.cancelled():
        task.exception()
