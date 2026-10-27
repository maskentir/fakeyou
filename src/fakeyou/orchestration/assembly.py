import asyncio
import os
import signal
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import (
    ExitStack,
    asynccontextmanager,
    contextmanager,
)
from dataclasses import dataclass, field
from typing import Any, Protocol

from graphiti_core import Graphiti
from langchain.agents.middleware import AgentMiddleware, ModelFallbackMiddleware
from langchain_core.tools import BaseTool

from fakeyou.audio.graph import TurnAudioTap
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.conversation.agent import CONTRIBUTION_GUIDANCE, create_meeting_agent
from fakeyou.conversation.contribution import SourceRegistry, source_registry
from fakeyou.conversation.control import ReplyControl
from fakeyou.conversation.conversation import (
    MeetingAgent,
    MeetingResponder,
    ToolResultCallback,
)
from fakeyou.conversation.grounding import GroundingVerifier, ModelGroundingVerifier
from fakeyou.conversation.meeting_messages import MeetingHistory, NoHistory
from fakeyou.conversation.meeting_turns import (
    MeetingTurns,
    TurnGeneration,
    TurnInteraction,
    TurnTiming,
    build_meeting_turns,
)
from fakeyou.conversation.middleware import (
    EvidenceRoutingMiddleware,
    SpeakerAttributionMiddleware,
)
from fakeyou.conversation.participation_graph import JudgeThresholds
from fakeyou.conversation.reply_planner import TurnLanguage
from fakeyou.conversation.session import SessionConfig
from fakeyou.conversation.turn_graph import CommandPort, ConversationPhase, PlannedTurn
from fakeyou.conversation.turn_judge import ModelTurnJudge, TurnJudge
from fakeyou.db.engine import open_database
from fakeyou.db.schema import require_current
from fakeyou.db.voiceprint_store import VoiceprintRepository, VoiceprintStore
from fakeyou.foundation.capabilities import EVIDENCE_TOOLS as REGISTRY_EVIDENCE_TOOLS
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.identity.verify import CalibrationArtifact, load_calibration_artifact
from fakeyou.identity.voiceprint_embed import SpeakerEmbedder, load_embedder
from fakeyou.identity.voiceprint_match import VoiceprintMatcher
from fakeyou.meet.meet_tools import create_action_status_tool
from fakeyou.memory.memory import (
    create_memory_prefetch_middleware,
    create_memory_tools,
    create_recall_error_middleware,
)
from fakeyou.memory.persona import (
    PersonaProfile,
    PersonaTool,
    meeting_tool_names,
    memory_wiring,
)
from fakeyou.observability.cleanup import TeardownStack
from fakeyou.observability.diagnostics import DEFAULT_DIAGNOSTICS, Diagnostics
from fakeyou.orchestration.kernel_port import KernelPort
from fakeyou.orchestration.persona_prompt import build_persona_prompt
from fakeyou.orchestration.providers import (
    create_chat_model,
    create_fallback_chat_model,
    create_strong_chat_model,
    create_turn_judge_model,
)
from fakeyou.perception.context import (
    PerceptionContext,
    create_perception_middleware,
)
from fakeyou.persistence.brief import MeetingBrief
from fakeyou.session.commands import CommandOutcome
from fakeyou.session.identity import PresenceFact
from fakeyou.session.intent import Reading, intent_of
from fakeyou.session.planner import plan_turn
from fakeyou.session.policy import UNATTRIBUTED, Channel, Grant, authorize

EVIDENCE_TOOLS = REGISTRY_EVIDENCE_TOOLS


@dataclass(slots=True)
class VoiceprintLive:
    tap: TurnAudioTap
    matcher: VoiceprintMatcher
    embedder: SpeakerEmbedder
    min_seconds: float
    timeout_seconds: float
    owners: Mapping[str, str]
    artifact: CalibrationArtifact | None = None
    slot_taps: dict[str, TurnAudioTap] = field(default_factory=dict)

    def tap_for(self, stream_id: str) -> TurnAudioTap:
        if not stream_id:
            return self.tap
        tap = self.slot_taps.get(stream_id)
        if tap is None:
            tap = TurnAudioTap()
            self.slot_taps[stream_id] = tap
        return tap

    def tap_for_slot(self, slot: int) -> TurnAudioTap:
        return self.tap_for(f"slot-{slot}")


def build_voiceprint_live(
    settings: Settings,
    *,
    store: VoiceprintRepository,
    embedder: SpeakerEmbedder,
    artifact: CalibrationArtifact | None,
) -> VoiceprintLive:
    return VoiceprintLive(
        tap=TurnAudioTap(),
        matcher=VoiceprintMatcher(
            store,
            threshold=settings.identity.voiceprint_threshold,
            min_margin=settings.identity.voiceprint_min_margin,
        ),
        embedder=embedder,
        min_seconds=settings.identity.voiceprint_min_seconds,
        timeout_seconds=settings.identity.voiceprint_timeout_seconds,
        owners={},
        artifact=artifact,
    )


@contextmanager
def open_voiceprint(settings: Settings) -> Iterator[VoiceprintLive | None]:
    if not settings.identity.voiceprint_enabled:
        yield None
        return
    with open_database(settings) as engine:
        require_current(engine)
        embedder = load_voiceprint_embedder(settings)
        artifact = _load_voiceprint_artifact(settings)
        yield build_voiceprint_live(
            settings, store=VoiceprintStore(engine), embedder=embedder, artifact=artifact
        )


def load_voiceprint_embedder(settings: Settings) -> SpeakerEmbedder:
    if not settings.identity.voiceprint_model_path:
        raise FakeYouError(ErrorCode.VOICEPRINT_MODEL_PATH_MISSING)
    return load_embedder(
        settings.identity.voiceprint_model_path, settings.identity.voiceprint_model_sha256
    )


def _load_voiceprint_artifact(settings: Settings) -> CalibrationArtifact | None:
    calibration_path = settings.identity.voiceprint_calibration_path
    if not calibration_path:
        return None
    artifact = load_calibration_artifact(calibration_path)
    if artifact is None:
        raise FakeYouError(ErrorCode.VOICEPRINT_CALIBRATION_INVALID)
    return artifact


def default_persona(settings: Settings) -> str:
    return build_persona_prompt(None, language=settings.speech.language)


@dataclass(frozen=True, slots=True)
class SignalHandlers:
    installed: tuple[str, ...]
    unavailable: tuple[str, ...]


@contextmanager
def stop_signal_handlers(
    stop: asyncio.Event, *, force_exit: Callable[[int], None] = os._exit
) -> Iterator[SignalHandlers]:
    loop = asyncio.get_running_loop()
    signaled = False

    def _handle(signum: int) -> None:
        nonlocal signaled
        if signaled:
            force_exit(128 + signum)
            return
        signaled = True
        stop.set()

    with ExitStack() as handlers:
        installed: list[str] = []
        unavailable: list[str] = []
        for candidate in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(candidate)
            try:
                loop.add_signal_handler(candidate, _handle, candidate)
            except (NotImplementedError, RuntimeError, ValueError):
                unavailable.append(signal.Signals(candidate).name)
                continue
            installed.append(signal.Signals(candidate).name)
            handlers.callback(signal.signal, candidate, previous)
            handlers.callback(loop.remove_signal_handler, candidate)
        yield SignalHandlers(installed=tuple(installed), unavailable=tuple(unavailable))


@dataclass(frozen=True, slots=True)
class AgentWiring:
    profile: PersonaProfile | None = None
    brief: MeetingBrief | None = None
    graphiti: Graphiti | None = None
    perception: PerceptionContext | None = None
    history: MeetingHistory = field(default_factory=NoHistory)


DEFAULT_AGENT_WIRING = AgentWiring()


class MeetCommands(CommandPort, Protocol):
    @property
    def available(self) -> bool: ...

    def recent(self) -> tuple[CommandOutcome, ...]: ...


@dataclass(frozen=True, slots=True)
class TurnBehavior:
    on_transition: Callable[[ConversationPhase, ConversationPhase, str], None]
    wake_phrases: Sequence[str] = ()
    twin_names: Sequence[str] = ()
    barge_in: bool = False
    on_tool_result: ToolResultCallback | None = None
    commands: MeetCommands | None = None
    kernel: KernelPort | None = None
    others_speaking: Callable[[], bool] | None = None
    presence: Callable[[], PresenceFact] | None = None


def build_turn_judge(settings: Settings, twin_names: Sequence[str]) -> TurnJudge | None:
    return ModelTurnJudge(
        create_turn_judge_model(settings),
        twin_names=twin_names,
        timeout_seconds=settings.turn_judge.timeout_seconds,
    )


def build_source_registry(
    profile: PersonaProfile | None, brief: MeetingBrief | None
) -> SourceRegistry:
    return source_registry(
        profile_facts=tuple(profile.verified_facts) if profile is not None else (),
        brief_facts={item.id: item.text for item in brief.approved_facts} if brief else None,
        brief_questions={item.id: item.text for item in brief.questions} if brief else None,
        brief_objectives={item.id: item.text for item in brief.objectives} if brief else None,
    )


def build_grounding_verifier(settings: Settings) -> GroundingVerifier:
    return ModelGroundingVerifier(
        create_turn_judge_model(settings),
        timeout_seconds=settings.turn_judge.timeout_seconds,
    )


class _ConversationBuilder:
    def __init__(
        self,
        settings: Settings,
        agent: AgentWiring,
        behavior: TurnBehavior,
        diagnostics: Diagnostics,
    ) -> None:
        self._settings = settings
        self._agent = agent
        self._behavior = behavior
        self._diagnostics = diagnostics
        self._tools: list[BaseTool] = []
        self._middleware: list[AgentMiddleware[Any, Any, Any]] = []
        self._persona = ""
        self._tool_names: frozenset[PersonaTool] = frozenset()
        self._control = ReplyControl()
        self._turn_language = TurnLanguage(settings.speech.language)

    def build_persona(self) -> None:
        profile = self._agent.profile
        if profile is None and self._agent.brief is not None:
            raise FakeYouError(ErrorCode.MEETING_BRIEF_REQUIRES_PROFILE)
        if profile is not None:
            self._tool_names = meeting_tool_names(
                profile, self._agent.brief, memory_requested=self._agent.graphiti is not None
            )
            profile = profile.model_copy(update={"allowed_tools": self._tool_names})
        self._persona = build_persona_prompt(
            profile,
            brief=self._agent.brief,
            recall_strategy=self._settings.memory.recall.strategy,
            language=self._settings.speech.language,
            perception=self._agent.perception is not None,
        )

    def add_routing(self) -> None:
        strong = create_strong_chat_model(self._settings)
        if strong is not None:
            self._middleware.append(
                EvidenceRoutingMiddleware(
                    strong,
                    evidence_tools=EVIDENCE_TOOLS,
                    journal=self._diagnostics.journal,
                )
            )

    def add_fallback(self) -> None:
        fallback = create_fallback_chat_model(self._settings)
        if fallback is not None:
            self._middleware.append(ModelFallbackMiddleware(fallback))

    def add_memory(self) -> None:
        profile = self._agent.profile
        graphiti = self._agent.graphiti
        if graphiti is None or profile is None or not self._tool_names:
            return
        bound, prefetch = memory_wiring(self._tool_names, self._settings.memory.recall.strategy)
        if prefetch:
            self._middleware.append(
                create_memory_prefetch_middleware(
                    graphiti,
                    group_id=profile.memory_group,
                    recall=self._settings.memory.recall,
                    diag=self._diagnostics,
                )
            )
        if bound:
            self._tools.extend(
                tool
                for tool in create_memory_tools(
                    graphiti, group_id=profile.memory_group, recall=self._settings.memory.recall
                )
                if tool.name in bound
            )
            self._middleware.append(create_recall_error_middleware(self._diagnostics.faults))

    def add_perception(self) -> None:
        perception = self._agent.perception
        if perception is None:
            return
        self._middleware.append(
            create_perception_middleware(perception, journal=self._diagnostics.journal)
        )

    def _grant(self) -> Grant:
        brief = self._agent.brief
        commands = self._behavior.commands
        return Grant(
            permissions=frozenset(brief.permissions) if brief is not None else frozenset(),
            browser_available=commands is not None and commands.available,
        )

    def _plan(self, event: TranscriptEvent, reading: Reading, channel: Channel) -> PlannedTurn:
        kernel = self._behavior.kernel
        actor = kernel.actor(event.id) if kernel is not None else UNATTRIBUTED
        authorization = authorize(
            intent_of(reading), actor=actor, channel=channel, grant=self._grant()
        )
        return PlannedTurn(
            plan_turn(authorization, channel=channel, plan_id=f"plan-{event.id}"),
            actor=actor.device_id or actor.kind,
        )

    def add_action_status(self) -> None:
        commands = self._behavior.commands
        if commands is not None:
            self._tools.append(create_action_status_tool(commands.recent))

    def add_speaker_attribution(self) -> None:
        self._middleware.append(SpeakerAttributionMiddleware())

    def build_agent(self, model: Any) -> Any:
        return create_meeting_agent(
            model,
            tools=self._tools,
            persona=self._persona,
            middleware=self._middleware,
        )

    def build_contributor(self, model: Any) -> Any:
        return create_meeting_agent(
            model,
            tools=self._tools,
            persona=f"{self._persona}\n\n{CONTRIBUTION_GUIDANCE}",
            middleware=self._middleware,
        )

    def build_responder(
        self,
        agent: MeetingAgent,
        contributor: Any | None = None,
        verifier: GroundingVerifier | None = None,
    ) -> MeetingResponder:
        return MeetingResponder(
            agent,
            self._agent.history,
            journal=self._diagnostics.journal,
            on_tool_result=self._behavior.on_tool_result,
            language=self._settings.speech.language,
            max_sentences=self._settings.conversation.max_spoken_sentences,
            history_max_tokens=self._settings.conversation.history_max_tokens,
            control=self._control,
            turn_language=self._turn_language,
            contributor=contributor,
            source_registry=build_source_registry(self._agent.profile, self._agent.brief),
            verifier=verifier,
            grounding_threshold=self._settings.perception.attention.grounding_confidence,
        )

    def build_turns(
        self,
        responder: MeetingResponder,
        *,
        participant_policy_enabled: bool = False,
    ) -> MeetingTurns:
        conversation_settings = self._settings.conversation
        judge_settings = self._settings.turn_judge
        names = tuple(dict.fromkeys((*self._behavior.wake_phrases, *self._behavior.twin_names)))
        return build_meeting_turns(
            responder,
            interaction=TurnInteraction(
                on_transition=self._behavior.on_transition,
                wake_phrases=self._behavior.wake_phrases,
                twin_names=self._behavior.twin_names,
                barge_in=self._behavior.barge_in,
                barge_in_min_words=conversation_settings.barge_in_min_words,
                barge_in_judge_enabled=conversation_settings.barge_in_judge_enabled,
                participant_policy_enabled=participant_policy_enabled,
                others_speaking=self._behavior.others_speaking,
                presence=self._behavior.presence,
                judge=build_turn_judge(self._settings, names),
                history=self._agent.history,
                judge_thresholds=JudgeThresholds(
                    reply=judge_settings.reply_threshold,
                    clarify=judge_settings.clarify_threshold,
                    barge_in=judge_settings.barge_in_threshold,
                    unnamed_reply=judge_settings.unnamed_reply_threshold,
                ),
                judge_window_lines=judge_settings.window_lines,
                planning=self._plan,
                commands=self._behavior.commands,
            ),
            timing=TurnTiming(
                response_timeout_seconds=conversation_settings.response_timeout_seconds,
                thinking_cue_after_seconds=(
                    conversation_settings.thinking_cue_after_seconds
                    if conversation_settings.thinking_cue_enabled
                    else None
                ),
            ),
            generation=TurnGeneration(
                max_spoken_sentences=conversation_settings.max_spoken_sentences,
                language=self._settings.speech.language,
                sentence_streaming=conversation_settings.sentence_streaming,
            ),
            diagnostics=Diagnostics(
                faults=self._diagnostics.faults, journal=self._diagnostics.journal
            ),
        )


@asynccontextmanager
async def open_conversation(
    settings: Settings,
    config: SessionConfig,
    *,
    agent: AgentWiring = DEFAULT_AGENT_WIRING,
    behavior: TurnBehavior,
    diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
) -> AsyncIterator[MeetingTurns]:
    builder = _ConversationBuilder(settings, agent, behavior, diagnostics)
    builder.build_persona()
    model = create_chat_model(settings)
    async with TeardownStack(faults=diagnostics.faults) as resources:
        builder.add_memory()
        builder.add_routing()
        builder.add_fallback()
        builder.add_perception()
        builder.add_action_status()
        builder.add_speaker_attribution()
        responder = builder.build_responder(
            builder.build_agent(model),
            builder.build_contributor(model) if config.attention_enabled else None,
            build_grounding_verifier(settings) if config.attention_enabled else None,
        )
        turns = builder.build_turns(
            responder, participant_policy_enabled=config.participant_policy_enabled
        )
        resources.push_async_callback(turns.lifecycle.close)
        yield turns
