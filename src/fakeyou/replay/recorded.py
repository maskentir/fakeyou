import asyncio
import json
import statistics
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast, override

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableConfig
from pydantic import PrivateAttr

from fakeyou.conversation.turn_judge import JudgeAction, JudgeWindow, TurnJudgement
from fakeyou.replay.fixture import JudgeVerdict, RecordedPlan, ReplyTiming, SynthesisTiming

CHUNK_BYTES = 4800
FILLER = "x"


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return float(statistics.median(values))


def _chunks(total: int) -> tuple[bytes, ...]:
    if total <= 0:
        return (b"",)
    return tuple(
        b"\x00" * min(CHUNK_BYTES, total - start) for start in range(0, total, CHUNK_BYTES)
    )


class RecordedSynthesizer:
    def __init__(self, timings: Mapping[tuple[str, int], SynthesisTiming]) -> None:
        self._timings = timings
        self._first_chunk_ms = _median([timing.first_chunk_ms for timing in timings.values()])
        self._duration_ms = _median(
            [timing.duration_ms for timing in timings.values() if timing.duration_ms is not None]
        )
        self._bytes_out = int(
            _median(
                [timing.bytes_out for timing in timings.values() if timing.bytes_out is not None]
            )
        )

    def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        del text, language
        return self._stream(sentence_index, transcript_id)

    async def _stream(
        self, sentence_index: int | None, transcript_id: str | None
    ) -> AsyncIterator[bytes]:
        timing = self._timing(sentence_index, transcript_id)
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

    def _timing(self, sentence_index: int | None, transcript_id: str | None) -> SynthesisTiming:
        recorded = None
        if transcript_id is not None and sentence_index is not None:
            recorded = self._timings.get((transcript_id, sentence_index))
        if recorded is None:
            return SynthesisTiming(self._first_chunk_ms, self._duration_ms, self._bytes_out)
        return SynthesisTiming(
            recorded.first_chunk_ms,
            recorded.duration_ms if recorded.duration_ms is not None else self._duration_ms,
            recorded.bytes_out if recorded.bytes_out is not None else self._bytes_out,
            recorded.chars,
        )

    async def aclose(self) -> None:
        return None


class RecordedJudge:
    def __init__(self, verdicts: Mapping[str, JudgeVerdict]) -> None:
        self._verdicts = verdicts
        self._delay_ms = _median([verdict.delay_ms for verdict in verdicts.values()])

    async def judge(self, window: JudgeWindow) -> TurnJudgement:
        verdict = self._verdicts.get(window.transcript_id) if window.transcript_id else None
        delay = verdict.delay_ms if verdict is not None else self._delay_ms
        await asyncio.sleep(delay / 1000)
        return _judgement(verdict)


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

    def __init__(
        self,
        replies: Mapping[str, ReplyTiming],
        plans: Mapping[str, RecordedPlan],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._replies = replies
        self._plans = plans

    @property
    @override
    def _llm_type(self) -> str:
        return "recorded-replay-model"

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
        del messages, stop, run_manager
        scripted = self._script(_transcript_id(kwargs))
        await asyncio.sleep((scripted.duration_ms or 0.0) / 1000)
        text = scripted.head + "".join(piece for _, piece in scripted.pieces) + scripted.tail
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    @override
    async def ainvoke(
        self,
        input: LanguageModelInput,
        config: RunnableConfig | None = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        kwargs["transcript_id"] = _config_transcript_id(config)
        return await super().ainvoke(input, config, stop=stop, **kwargs)

    @override
    async def astream(
        self,
        input: LanguageModelInput,
        config: RunnableConfig | None = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AIMessageChunk]:
        kwargs["transcript_id"] = _config_transcript_id(config)
        async for chunk in super().astream(input, config, stop=stop, **kwargs):
            yield chunk

    @override
    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        del messages, stop, run_manager
        scripted = self._script(_transcript_id(kwargs))
        yield _chunk(scripted.head)
        elapsed = 0.0
        for offset_ms, piece in scripted.pieces:
            await asyncio.sleep(max(0.0, offset_ms - elapsed) / 1000)
            elapsed = offset_ms
            yield _chunk(piece)
        yield _chunk(scripted.tail)
        if scripted.duration_ms is not None:
            await asyncio.sleep(max(0.0, scripted.duration_ms - elapsed) / 1000)

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
            pieces.append((sentence.offset_ms, piece))
        return ScriptedReply(_head(plan), tuple(pieces), '"}', reply.duration_ms)


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


def _transcript_id(kwargs: Mapping[str, Any]) -> str | None:
    value = kwargs.get("transcript_id")
    return value if isinstance(value, str) else None


def _config_transcript_id(config: RunnableConfig | None) -> str | None:
    metadata = (config or {}).get("metadata") or {}
    value = metadata.get("transcript_id")
    return value if isinstance(value, str) else None
