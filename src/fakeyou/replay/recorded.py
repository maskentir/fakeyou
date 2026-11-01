import asyncio
import json
import statistics
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast, override

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

from fakeyou.conversation.turn_judge import JudgeAction, JudgeWindow, TurnJudgement
from fakeyou.foundation.taxonomy import ProviderFaultKind
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.replay.fixture import (
    JudgeVerdict,
    RecordedPlan,
    ReplyTiming,
    SpokenFinal,
    SynthesisFault,
    SynthesisTiming,
)
from fakeyou.replay.pacer import BYTES_PER_SECOND
from fakeyou.speech.contract import SpeechTurn
from fakeyou.speech.streaming import SentenceTurn, SpeechTag, journaled_stream

CHUNK_BYTES = 4800
FILLER = "x"
RECORDED_PROVIDER = "recorded"
VOICED_SAMPLE = (1000).to_bytes(2, "little", signed=True)


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(statistics.median(values))


def _chunks(total: int) -> tuple[bytes, ...]:
    total -= total % len(VOICED_SAMPLE)
    if total <= 0:
        return (b"",)
    return tuple(
        VOICED_SAMPLE * (min(CHUNK_BYTES, total - start) // len(VOICED_SAMPLE))
        for start in range(0, total, CHUNK_BYTES)
    )


async def _failing(fault: SynthesisFault) -> AsyncGenerator[bytes]:
    await asyncio.sleep(fault.duration_ms / 1000)
    raise ProviderFault("tts", cast(ProviderFaultKind, fault.kind))
    yield b""


def _normalized(text: str) -> str:
    kept = "".join(char for char in text.casefold() if char.isalnum() or char.isspace())
    return " ".join(kept.split())


class RecordedSynthesizer:
    def __init__(
        self,
        timings: Mapping[tuple[str, int], SynthesisTiming],
        *,
        faults: Mapping[tuple[str, int], tuple[SynthesisFault, ...]] | None = None,
        journal: Journal | None = None,
    ) -> None:
        self._timings = timings
        self._faults = {key: list(recorded) for key, recorded in (faults or {}).items()}
        self._journal = journal
        complete = [
            timing
            for timing in timings.values()
            if timing.duration_ms is not None and timing.bytes_out
        ]
        self._first_chunk_ms = _median([timing.first_chunk_ms for timing in timings.values()])
        self._chars_per_second = _median(
            [
                timing.chars / (timing.bytes_out / BYTES_PER_SECOND)
                for timing in complete
                if timing.chars and timing.bytes_out
            ]
        )
        self._transfer_ms_per_byte = _median(
            [
                ((timing.duration_ms or 0.0) - timing.first_chunk_ms) / timing.bytes_out
                for timing in complete
                if timing.bytes_out
            ]
        )

    def open_turn(
        self, *, transcript_id: str | None = None, language: str | None = None
    ) -> SpeechTurn:
        return SentenceTurn(self.stream, transcript_id=transcript_id, language=language)

    def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        del language
        tag = SpeechTag(RECORDED_PROVIDER, sentence_index, transcript_id)
        pending = (
            self._faults.get((transcript_id, sentence_index or 0))
            if transcript_id is not None
            else None
        )
        if pending:
            fault = pending.pop(0)
            return journaled_stream(self._journal, text, lambda _text: _failing(fault), tag)
        timing = self._timing(text, sentence_index, transcript_id)
        return journaled_stream(self._journal, text, lambda _text: self._stream(timing), tag)

    async def _stream(self, timing: SynthesisTiming) -> AsyncGenerator[bytes]:
        chunks = _chunks(timing.bytes_out or 0)
        loop = asyncio.get_running_loop()
        origin = loop.time()
        span = max(0.0, (timing.duration_ms or 0.0) - timing.first_chunk_ms)
        step = span / (len(chunks) - 1) if len(chunks) > 1 else 0.0
        for index, chunk in enumerate(chunks):
            target = origin + (timing.first_chunk_ms + step * index) / 1000
            delay = target - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            yield chunk

    def _timing(
        self, text: str, sentence_index: int | None, transcript_id: str | None
    ) -> SynthesisTiming:
        recorded = (
            self._timings.get((transcript_id, sentence_index or 0))
            if transcript_id is not None
            else None
        )
        first_chunk_ms = recorded.first_chunk_ms if recorded is not None else self._first_chunk_ms
        if recorded is not None and recorded.bytes_out and recorded.duration_ms is not None:
            return recorded
        spoken = len(text) / self._chars_per_second if self._chars_per_second else 0.0
        bytes_out = round(spoken * BYTES_PER_SECOND)
        duration_ms = first_chunk_ms + bytes_out * self._transfer_ms_per_byte
        return SynthesisTiming(first_chunk_ms, duration_ms, bytes_out, len(text))

    async def aclose(self) -> None:
        return None


class RecordedJudge:
    def __init__(
        self,
        verdicts: Mapping[str, JudgeVerdict],
        finals: Sequence[SpokenFinal] = (),
        *,
        origin: float = 0.0,
    ) -> None:
        self._verdicts = verdicts
        self._origin = origin
        self._delay_ms = _median([verdict.delay_ms for verdict in verdicts.values()])
        self._offsets = {final.entry.id: final.offset for final in finals}
        self._by_text: dict[str, list[SpokenFinal]] = {}
        for final in sorted(finals, key=lambda final: final.offset):
            self._by_text.setdefault(_normalized(final.entry.text), []).append(final)

    async def judge(self, window: JudgeWindow) -> TurnJudgement:
        loop = asyncio.get_running_loop()
        transcript_id = window.transcript_id or self._resolve(window.latest.text, loop.time())
        verdict = self._verdicts.get(transcript_id) if transcript_id is not None else None
        offset = self._offsets.get(transcript_id) if transcript_id is not None else None
        if verdict is None or offset is None:
            await asyncio.sleep(self._delay_ms / 1000)
            return _unfinished(window.latest.text)
        delay = self._origin + offset + verdict.delay_ms / 1000 - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        return _judgement(verdict)

    def _resolve(self, text: str, now: float) -> str | None:
        upcoming = [
            final
            for final in self._by_text.get(_normalized(text), ())
            if self._origin + final.offset >= now
        ]
        return upcoming[0].entry.id if upcoming else None


def _unfinished(text: str) -> TurnJudgement:
    return TurnJudgement(
        heard=text,
        finished=False,
        backchannel=False,
        addressee="unclear",
        action="stay_silent",
        confidence=0.0,
    )


def _judgement(verdict: JudgeVerdict | None) -> TurnJudgement:
    if verdict is None:
        return TurnJudgement(
            heard="",
            finished=True,
            backchannel=False,
            addressee="unclear",
            action="stay_silent",
            confidence=0.0,
        )
    fields = verdict.fields
    return TurnJudgement.model_validate(
        {
            "heard": str(fields.get("heard", "")),
            "finished": bool(fields.get("finished", True)),
            "backchannel": bool(fields.get("backchannel", False)),
            "logistics_check": bool(fields.get("logistics_check", False)),
            "addressee": fields.get("addressee", "unclear"),
            "action": _action(fields),
            "confidence": float(fields.get("confidence", 0.0)),
            "requests_chat": bool(fields.get("requests_chat", False)),
            "meet_action": fields.get("meet_action", "none"),
            "action_only": bool(fields.get("action_only", False)),
            "evidence": tuple(fields.get("evidence_needs", ())),
            "state_question": fields.get("state_question", "none"),
            "reaction_emoji": fields.get("reaction_emoji"),
        }
    )


def _action(fields: Mapping[str, Any]) -> JudgeAction:
    reason = str(fields.get("reason", ""))
    suffix = reason.removeprefix("judge_")
    if suffix in ("reply", "clarify", "stay_silent"):
        return cast(JudgeAction, suffix)
    return "stay_silent"


@dataclass(frozen=True, slots=True)
class ScriptedReply:
    head: str
    pieces: tuple[tuple[float, str], ...]
    tail: str
    duration_ms: float | None


class RecordedModel(BaseChatModel):
    _replies: Mapping[str, ReplyTiming] = PrivateAttr(default_factory=dict)
    _plans: Mapping[str, RecordedPlan] = PrivateAttr(default_factory=dict)
    _latency_scale: float = PrivateAttr(default=1.0)

    def __init__(
        self,
        replies: Mapping[str, ReplyTiming],
        plans: Mapping[str, RecordedPlan],
        *,
        latency_scale: float = 1.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._replies = replies
        self._plans = plans
        self._latency_scale = latency_scale

    @property
    @override
    def _llm_type(self) -> str:
        return "recorded-replay-model"

    @override
    def bind_tools(self, tools: Any, **kwargs: Any) -> "RecordedModel":
        del tools, kwargs
        return self

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del messages, stop, run_manager, kwargs
        scripted = self._script(None)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=scripted.head + scripted.tail))]
        )

    @override
    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        del stop, run_manager, kwargs
        scripted = self._script(self._transcript_id(messages))
        await asyncio.sleep((scripted.duration_ms or 0.0) / 1000)
        text = scripted.head + "".join(piece for _, piece in scripted.pieces) + scripted.tail
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    @override
    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del stop, run_manager, kwargs
        scripted = self._script(self._transcript_id(messages))
        yield _chunk(scripted.head)
        elapsed = 0.0
        for offset_ms, piece in scripted.pieces:
            await asyncio.sleep(max(0.0, offset_ms - elapsed) / 1000)
            elapsed = offset_ms
            yield _chunk(piece)
        yield _chunk(scripted.tail)
        if scripted.duration_ms is not None:
            await asyncio.sleep(max(0.0, scripted.duration_ms - elapsed) / 1000)

    def _transcript_id(self, messages: Sequence[BaseMessage]) -> str | None:
        latest = next(
            (message for message in reversed(messages) if isinstance(message, HumanMessage)), None
        )
        identifier = latest.id if latest is not None else None
        return identifier if isinstance(identifier, str) else None

    def _script(self, transcript_id: str | None) -> ScriptedReply:
        reply = self._replies.get(transcript_id) if transcript_id else None
        plan = self._plans.get(transcript_id) if transcript_id else None
        if reply is None or plan is None:
            silent = RecordedPlan(
                language="en",
                intent="silent",
                contribution="none",
                cites=(),
                structured=True,
                replaced=False,
            )
            return ScriptedReply(_head(silent), (), '"}', None)
        pieces: list[tuple[float, str]] = []
        sentences = reply.sentences
        for index, sentence in enumerate(sentences):
            text = sentence.text if sentence.text is not None else FILLER * max(1, sentence.chars)
            piece = json.dumps(text)[1:-1]
            if index + 1 < len(sentences):
                piece += " "
            pieces.append((sentence.offset_ms * self._latency_scale, piece))
        duration_ms = (
            reply.duration_ms * self._latency_scale if reply.duration_ms is not None else None
        )
        return ScriptedReply(_head(plan), tuple(pieces), '"}', duration_ms)


def _head(plan: RecordedPlan) -> str:
    body = json.dumps(
        {
            "understood": "",
            "language": plan.language,
            "intent": plan.intent,
            "contribution": plan.contribution,
            "cites": list(plan.cites),
            "commits_owner": False,
        }
    )
    return f'{body[:-1]}, "text": "'


def _chunk(text: str) -> ChatGenerationChunk:
    return ChatGenerationChunk(message=AIMessageChunk(content=text))
