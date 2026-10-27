import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.control import ReplyControl
from fakeyou.conversation.conversation import (
    ConversationResponder,
    StreamingConversationResponder,
)
from fakeyou.conversation.meeting_messages import MeetingHistory, NoHistory
from fakeyou.conversation.model_responder import ReplierStreamResponder, ReplierWholeResponder
from fakeyou.conversation.participation_graph import JudgeThresholds, TurnJudging
from fakeyou.conversation.reply_planner import TurnLanguage
from fakeyou.conversation.session import MeetingAudio
from fakeyou.conversation.thinking_cue import thinking_cue_for
from fakeyou.conversation.turn_graph import (
    ChatChannel,
    MeetActionExecutor,
    Responder,
    SentenceLimits,
    StreamTurnFactory,
    ToolStartHook,
    TurnSettler,
    WholeResponder,
    WholeTurnFactory,
    build_turn_graph,
)
from fakeyou.conversation.turn_ingress import (
    IngressTiming,
    PolicyOptions,
    TurnIngress,
    addressing_phrases,
)
from fakeyou.conversation.turn_judge import JudgeCache, TurnJudge, WindowSource
from fakeyou.conversation.turn_lifecycle import (
    DeferredOptions,
    ReplyOptions,
    TransitionObserver,
    TurnLifecycle,
    reply_mode,
)
from fakeyou.conversation.turn_recovery import RecoveryOptions
from fakeyou.foundation.taxonomy import Subsystem
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.foundation.triggers import ReplyChannel
from fakeyou.observability.diagnostics import DEFAULT_DIAGNOSTICS, Diagnostics
from fakeyou.session.identity import PresenceFact

MeetingReplier = ConversationResponder | StreamingConversationResponder


@dataclass(frozen=True, slots=True)
class TurnInteraction:
    on_transition: TransitionObserver | None = None
    wake_phrases: Sequence[str] = ()
    twin_names: Sequence[str] = ()
    barge_in: bool = False
    barge_in_min_words: int = DEFAULTS.conversation.barge_in_min_words
    barge_in_judge_enabled: bool = True
    participant_policy_enabled: bool = False
    others_speaking: Callable[[], bool] | None = None
    presence: Callable[[], PresenceFact] | None = None
    judge: TurnJudge | None = None
    history: MeetingHistory = NoHistory()
    judge_thresholds: JudgeThresholds = JudgeThresholds(
        reply=DEFAULTS.turn_judge.reply_threshold,
        clarify=DEFAULTS.turn_judge.clarify_threshold,
        barge_in=DEFAULTS.turn_judge.barge_in_threshold,
        unnamed_reply=DEFAULTS.turn_judge.unnamed_reply_threshold,
    )
    judge_window_lines: int = DEFAULTS.turn_judge.window_lines
    shadow: bool = False
    chat: ChatChannel | None = None
    settle_turn: TurnSettler | None = None
    meet_action_executor: MeetActionExecutor | None = None
    control: ReplyControl | None = None


@dataclass(frozen=True, slots=True)
class TurnTiming:
    response_timeout_seconds: float = DEFAULTS.conversation.response_timeout_seconds
    quiet_gap_seconds: float = DEFAULTS.perception.attention.floor_gap_seconds
    deferred_ttl_seconds: float = DEFAULTS.perception.attention.ttl_seconds
    thinking_cue_after_seconds: float | None = None
    clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True, slots=True)
class TurnGeneration:
    max_spoken_sentences: int = DEFAULTS.conversation.max_spoken_sentences
    language: str = DEFAULTS.speech.language
    sentence_streaming: bool = DEFAULTS.conversation.sentence_streaming
    turn_language: TurnLanguage | None = None


DEFAULT_TURN_INTERACTION = TurnInteraction()
DEFAULT_TURN_TIMING = TurnTiming()
DEFAULT_TURN_GENERATION = TurnGeneration()


@dataclass(frozen=True, slots=True)
class MeetingTurns:
    lifecycle: TurnLifecycle
    ingress: TurnIngress
    responder: MeetingReplier

    @property
    def is_busy(self) -> bool:
        return self.lifecycle.is_busy

    def receive_chat(self, event: TranscriptEvent) -> bool:
        return self.ingress.receive_chat(event)

    def receive_attention(self, event: TranscriptEvent, *, channel: ReplyChannel) -> bool:
        return self.ingress.receive_attention(event, channel=channel)

    def bind(self, session: MeetingAudio) -> None:
        self.lifecycle.bind(session)

    async def close(self) -> None:
        await self.lifecycle.close()

    def raise_if_failed(self) -> None:
        self.lifecycle.raise_if_failed()

    async def pause(self) -> None:
        await self.lifecycle.pause()

    def resume(self) -> None:
        self.lifecycle.resume()


class _SilentJournal:
    def record(self, event: str, *, source: Subsystem, **fields: Any) -> None:
        del event, source, fields


def _whole_turn(replier: ConversationResponder) -> WholeTurnFactory:
    def whole_turn(events: tuple[TranscriptEvent, ...], hook: ToolStartHook) -> WholeResponder:
        return ReplierWholeResponder(replier, events, hook)

    return whole_turn


def _stream_turn(replier: StreamingConversationResponder) -> StreamTurnFactory:
    def stream_turn(
        events: tuple[TranscriptEvent, ...], hook: ToolStartHook, request_id: str
    ) -> Responder:
        return ReplierStreamResponder(replier.stream_sentences, events, hook, request_id)

    return stream_turn


def _reply_options(replier: MeetingReplier, generation: TurnGeneration) -> ReplyOptions:
    streams = reply_mode(sentence_streaming=generation.sentence_streaming) == "streamed"
    if streams and isinstance(replier, StreamingConversationResponder):
        return ReplyOptions(stream_turn=_stream_turn(replier))
    if not isinstance(replier, ConversationResponder):
        raise TypeError("a stream-only responder cannot answer without sentence streaming")
    return ReplyOptions(
        whole_turn=_whole_turn(replier),
        sentences=SentenceLimits(
            max_sentences=generation.max_spoken_sentences, language=generation.language
        ),
    )


def _deferred_options(
    replier: MeetingReplier, interaction: TurnInteraction, timing: TurnTiming
) -> DeferredOptions:
    chat_turn = _whole_turn(replier) if isinstance(replier, ConversationResponder) else None
    return DeferredOptions(
        quiet_gap_seconds=timing.quiet_gap_seconds,
        ttl_seconds=timing.deferred_ttl_seconds,
        chat=interaction.chat,
        chat_turn=chat_turn,
        settle_turn=interaction.settle_turn,
        meet_action_executor=interaction.meet_action_executor,
        others_speaking=interaction.others_speaking,
    )


def _judging(interaction: TurnInteraction) -> TurnJudging | None:
    if interaction.judge is None:
        return None
    return TurnJudging(
        cache=JudgeCache(interaction.judge),
        windows=WindowSource(
            interaction.history,
            interaction.judge_window_lines,
            twin_names=tuple(dict.fromkeys((*interaction.wake_phrases, *interaction.twin_names))),
            presence=interaction.presence,
        ),
        thresholds=interaction.judge_thresholds,
    )


def build_meeting_turns(
    replier: MeetingReplier,
    *,
    interaction: TurnInteraction = DEFAULT_TURN_INTERACTION,
    timing: TurnTiming = DEFAULT_TURN_TIMING,
    generation: TurnGeneration = DEFAULT_TURN_GENERATION,
    diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
) -> MeetingTurns:
    language = generation.turn_language
    reply = _reply_options(replier, generation)
    lifecycle = TurnLifecycle(
        build_turn_graph(),
        journal=_SilentJournal() if diagnostics.journal is None else diagnostics.journal,
        judging=_judging(interaction),
        clock=timing.clock,
    )
    lifecycle.configure_control(interaction.control)
    lifecycle.configure_recovery(
        RecoveryOptions(
            faults=diagnostics.faults,
            language=generation.language if language is None else language.read,
            response_timeout_seconds=timing.response_timeout_seconds,
        )
    )
    lifecycle.configure_reply(reply)
    lifecycle.configure_deferred(_deferred_options(replier, interaction, timing))
    lifecycle.configure_barge_in(enabled=interaction.barge_in)
    lifecycle.configure_shadow(enabled=interaction.shadow)
    lifecycle.configure_cue(
        thinking_cue_for(
            generation.language,
            timing.thinking_cue_after_seconds,
            None if language is None else language.read,
        )
    )
    if interaction.on_transition is not None:
        lifecycle.observe(interaction.on_transition)
    wake_phrases = addressing_phrases(interaction.wake_phrases)
    ingress = TurnIngress(
        lifecycle,
        addressing_names=wake_phrases or addressing_phrases(interaction.twin_names),
        wake_phrases=wake_phrases,
        timing=IngressTiming(
            barge_in_min_words=interaction.barge_in_min_words,
            clock=timing.clock,
        ),
        barge_in_judge_enabled=interaction.barge_in_judge_enabled,
    )
    ingress.configure_policy(
        PolicyOptions(
            enabled=interaction.participant_policy_enabled,
            others_speaking=interaction.others_speaking,
        )
    )
    return MeetingTurns(lifecycle=lifecycle, ingress=ingress, responder=replier)
