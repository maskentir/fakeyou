import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.conversation import (
    ConversationResponder,
    StreamingConversationResponder,
)
from fakeyou.conversation.jev_refresh import JevState
from fakeyou.conversation.model_responder import ReplierStreamResponder, ReplierWholeResponder
from fakeyou.conversation.participation_decider import Decider
from fakeyou.conversation.session import MeetingAudio
from fakeyou.conversation.turn_graph import (
    Responder,
    SentenceLimits,
    StreamTurnFactory,
    ToolStartHook,
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
from fakeyou.conversation.turn_lifecycle import (
    ReplyOptions,
    TransitionObserver,
    TurnLifecycle,
    reply_mode,
)
from fakeyou.conversation.turn_recovery import RecoveryOptions
from fakeyou.foundation.taxonomy import Subsystem
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.diagnostics import DEFAULT_DIAGNOSTICS, Diagnostics

MeetingReplier = ConversationResponder | StreamingConversationResponder


@dataclass(frozen=True, slots=True)
class TurnInteraction:
    on_transition: TransitionObserver | None = None
    wake_phrases: Sequence[str] = ()
    twin_names: Sequence[str] = ()
    barge_in: bool = False
    barge_in_min_words: int = DEFAULTS.conversation.barge_in_min_words
    participant_policy_enabled: bool = False
    others_speaking: Callable[[], bool] | None = None
    decider: Decider | None = None
    speak_threshold: float = DEFAULTS.participation_decider.speak_threshold
    clarify_threshold: float = DEFAULTS.participation_decider.clarify_threshold
    window_turns: int = DEFAULTS.participation_decider.window_turns
    shadow: bool = False


@dataclass(frozen=True, slots=True)
class TurnTiming:
    response_timeout_seconds: float = DEFAULTS.conversation.response_timeout_seconds
    activation_seconds: float = DEFAULTS.conversation.activation_seconds
    clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True, slots=True)
class TurnGeneration:
    max_spoken_sentences: int = DEFAULTS.conversation.max_spoken_sentences
    language: str = DEFAULTS.speech.language
    sentence_streaming: bool = DEFAULTS.conversation.sentence_streaming


DEFAULT_TURN_INTERACTION = TurnInteraction()
DEFAULT_TURN_TIMING = TurnTiming()
DEFAULT_TURN_GENERATION = TurnGeneration()


@dataclass(frozen=True, slots=True)
class MeetingTurns:
    lifecycle: TurnLifecycle
    ingress: TurnIngress
    responder: MeetingReplier
    decider: Decider | None = None

    @property
    def is_busy(self) -> bool:
        return self.lifecycle.is_busy

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


def _jev_state(interaction: TurnInteraction) -> JevState | None:
    if interaction.decider is None:
        return None
    return JevState(
        speak_threshold=interaction.speak_threshold,
        clarify_threshold=interaction.clarify_threshold,
    )


def build_meeting_turns(
    replier: MeetingReplier,
    *,
    interaction: TurnInteraction = DEFAULT_TURN_INTERACTION,
    timing: TurnTiming = DEFAULT_TURN_TIMING,
    generation: TurnGeneration = DEFAULT_TURN_GENERATION,
    diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
) -> MeetingTurns:
    reply = _reply_options(replier, generation)
    lifecycle = TurnLifecycle(
        build_turn_graph(decider=interaction.decider, window_turns=interaction.window_turns),
        journal=_SilentJournal() if diagnostics.journal is None else diagnostics.journal,
        jev=_jev_state(interaction),
        clock=timing.clock,
    )
    lifecycle.configure_recovery(
        RecoveryOptions(
            faults=diagnostics.faults,
            language=generation.language,
            response_timeout_seconds=timing.response_timeout_seconds,
        )
    )
    lifecycle.configure_reply(reply)
    lifecycle.configure_barge_in(enabled=interaction.barge_in)
    lifecycle.configure_shadow(enabled=interaction.shadow)
    if interaction.on_transition is not None:
        lifecycle.observe(interaction.on_transition)
    wake_phrases = addressing_phrases(interaction.wake_phrases)
    ingress = TurnIngress(
        lifecycle,
        addressing_names=wake_phrases or addressing_phrases(interaction.twin_names),
        wake_phrases=wake_phrases,
        timing=IngressTiming(
            activation_seconds=timing.activation_seconds,
            barge_in_min_words=interaction.barge_in_min_words,
            clock=timing.clock,
        ),
    )
    ingress.configure_policy(
        PolicyOptions(
            enabled=interaction.participant_policy_enabled,
            others_speaking=interaction.others_speaking,
        )
    )
    return MeetingTurns(
        lifecycle=lifecycle, ingress=ingress, responder=replier, decider=interaction.decider
    )
