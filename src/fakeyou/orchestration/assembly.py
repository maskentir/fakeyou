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
from typing import Any

from graphiti_core import Graphiti
from langchain.agents.middleware import AgentMiddleware, ModelFallbackMiddleware
from langchain_core.tools import BaseTool

from fakeyou.audio.graph import TurnAudioTap
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import (
    Settings,
)
from fakeyou.conversation.agent import (
    ENDING_RULE,
    MEETING_GUIDANCE,
    SPOKEN_LANGUAGE_GUIDANCE,
    VISUAL_GUIDANCE,
    create_meeting_agent,
    create_visual_tools,
)
from fakeyou.conversation.conversation import (
    ConversationLoop,
    ConversationPhase,
    ConversationResponder,
    LangGraphResponder,
    LoopGeneration,
    LoopInteraction,
    LoopTiming,
    MeetingAgent,
    StreamingLangGraphResponder,
    ToolResultCallback,
    conversation_thread_id,
)
from fakeyou.conversation.middleware import HistoryTrimMiddleware, OrphanToolCallMiddleware
from fakeyou.conversation.session import SessionConfig
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.db.engine import configured_url, open_database, require_url
from fakeyou.db.schema import require_current
from fakeyou.db.voiceprint_store import VoiceprintRepository, VoiceprintStore
from fakeyou.foundation.greetings import create_greeting_middleware
from fakeyou.identity.verify import CalibrationArtifact, load_calibration_artifact
from fakeyou.identity.voiceprint_embed import SpeakerEmbedder, load_embedder
from fakeyou.identity.voiceprint_match import VoiceprintMatcher
from fakeyou.memory.memory import (
    VisualCaptionResolver,
    create_meet_action_tools,
    create_memory_prefetch_middleware,
    create_memory_tools,
    create_recall_error_middleware,
)
from fakeyou.memory.persona import (
    PersonaProfile,
    PersonaTool,
    build_system_prompt,
    meeting_tool_names,
    memory_wiring,
    refuse_meet_tool,
)
from fakeyou.memory.visual_hydrate import create_visual_hydrate_middleware, create_visual_resolver
from fakeyou.observability.cleanup import TeardownStack
from fakeyou.observability.diagnostics import DEFAULT_DIAGNOSTICS, Diagnostics
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.providers import (
    create_chat_model,
    create_fallback_chat_model,
    create_visual_chat_model,
)
from fakeyou.orchestration.runtime import MeetActionDispatcher
from fakeyou.persistence.brief import MeetingBrief
from fakeyou.visual.visual_caption import VisualCaptioner
from fakeyou.visual.visual_store import VisualStore


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
    return (
        f"{MEETING_GUIDANCE} {SPOKEN_LANGUAGE_GUIDANCE[settings.speech.language]}\n\n{ENDING_RULE}"
    )


def create_visual_captioner(
    settings: Settings, journal: Journal | None = None
) -> VisualCaptioner | None:
    if not settings.visual.enabled:
        return None
    return VisualCaptioner(
        create_visual_chat_model(settings), settings=settings.visual, journal=journal
    )


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
    visual_store: VisualStore | None = None
    visual_captioner: VisualCaptioner | None = None


DEFAULT_AGENT_WIRING = AgentWiring()


@dataclass(frozen=True, slots=True)
class LoopBehavior:
    on_transition: Callable[[ConversationPhase, ConversationPhase, str], None]
    wake_phrases: Sequence[str] = ()
    twin_names: Sequence[str] = ()
    barge_in: bool = False
    on_tool_result: ToolResultCallback | None = None
    meet_dispatcher: MeetActionDispatcher | None = None
    others_speaking: Callable[[], bool] | None = None


def _visual_wired(settings: Settings, agent: AgentWiring) -> bool:
    return (
        settings.visual.enabled
        and agent.visual_store is not None
        and agent.visual_captioner is not None
    )


class _ConversationBuilder:
    def __init__(
        self,
        settings: Settings,
        agent: AgentWiring,
        behavior: LoopBehavior,
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

    def build_persona(self) -> None:
        profile = self._agent.profile
        if profile is None:
            if self._agent.brief is not None:
                raise FakeYouError(ErrorCode.MEETING_BRIEF_REQUIRES_PROFILE)
            self._persona = default_persona(self._settings)
        else:
            self._tool_names = meeting_tool_names(
                profile, self._agent.brief, memory_requested=self._agent.graphiti is not None
            )
            self._persona = build_system_prompt(
                profile.model_copy(update={"allowed_tools": self._tool_names}),
                brief=self._agent.brief,
                recall_strategy=self._settings.memory.recall.strategy,
            )
        if _visual_wired(self._settings, self._agent):
            self._persona = f"{self._persona}\n\n{VISUAL_GUIDANCE}"

    def add_greeting(self) -> None:
        profile = self._agent.profile
        language = profile.language if profile is not None else self._settings.speech.language
        self._middleware.append(create_greeting_middleware(language))
        fallback = create_fallback_chat_model(self._settings)
        if fallback is not None:
            self._middleware.append(ModelFallbackMiddleware(fallback))

    def add_history(self) -> None:
        self._middleware.append(
            HistoryTrimMiddleware(max_tokens=self._settings.conversation.history_max_tokens)
        )
        self._middleware.append(OrphanToolCallMiddleware())

    def wired_visual(self) -> VisualCaptionResolver | None:
        if not _visual_wired(self._settings, self._agent):
            return None
        store = self._agent.visual_store
        captioner = self._agent.visual_captioner
        assert store is not None
        assert captioner is not None
        return create_visual_resolver(store, captioner)

    def add_memory(self, visual: VisualCaptionResolver | None) -> None:
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
                    visual_caption=visual,
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

    def add_visual(self) -> None:
        if not _visual_wired(self._settings, self._agent):
            return
        store = self._agent.visual_store
        captioner = self._agent.visual_captioner
        assert store is not None
        assert captioner is not None
        self._middleware.append(
            create_visual_hydrate_middleware(
                store,
                captioner,
                max_images=self._settings.visual.max_images,
                journal=self._diagnostics.journal,
            )
        )
        self._tools.extend(create_visual_tools(store, captioner))

    def add_meet_tools(self) -> None:
        if self._agent.profile is None or self._behavior.meet_dispatcher is None:
            return
        meet = self._tool_names - {"recall_meeting_context"}
        if meet:
            self._tools.extend(
                tool
                for tool in create_meet_action_tools(
                    self._behavior.meet_dispatcher,
                    refuse=refuse_meet_tool,
                    journal=self._diagnostics.journal,
                )
                if tool.name in meet
            )

    def build_agent(self, model: Any, checkpointer: Any) -> Any:
        return create_meeting_agent(
            model,
            tools=self._tools,
            checkpointer=checkpointer,
            persona=self._persona,
            middleware=self._middleware,
        )

    def build_responder(self, agent: MeetingAgent, thread_id: str) -> ConversationResponder:
        if self._settings.conversation.sentence_streaming:
            return StreamingLangGraphResponder(
                agent,
                thread_id,
                journal=self._diagnostics.journal,
                on_tool_result=self._behavior.on_tool_result,
                language=self._settings.speech.language,
                max_sentences=self._settings.conversation.max_spoken_sentences,
            )
        return LangGraphResponder(
            agent,
            thread_id,
            journal=self._diagnostics.journal,
            on_tool_result=self._behavior.on_tool_result,
        )

    def build_loop(self, responder: ConversationResponder) -> ConversationLoop:
        conversation_settings = self._settings.conversation
        return ConversationLoop(
            responder,
            interaction=LoopInteraction(
                on_transition=self._behavior.on_transition,
                wake_phrases=self._behavior.wake_phrases,
                twin_names=self._behavior.twin_names,
                barge_in=self._behavior.barge_in,
                barge_in_min_words=conversation_settings.barge_in_min_words,
                participant_policy_enabled=conversation_settings.participant_policy_enabled,
                others_speaking=self._behavior.others_speaking,
            ),
            timing=LoopTiming(
                response_timeout_seconds=conversation_settings.response_timeout_seconds,
                activation_seconds=conversation_settings.activation_seconds,
            ),
            generation=LoopGeneration(
                max_spoken_sentences=conversation_settings.max_spoken_sentences,
                language=self._settings.speech.language,
                sentence_streaming=conversation_settings.sentence_streaming,
                pre_generate_enabled=conversation_settings.pre_generate_enabled,
                pregen_fuzzy_restart_enabled=conversation_settings.pregen_fuzzy_restart_enabled,
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
    behavior: LoopBehavior,
    diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
) -> AsyncIterator[ConversationLoop]:
    builder = _ConversationBuilder(settings, agent, behavior, diagnostics)
    builder.build_persona()
    model = create_chat_model(settings)
    async with TeardownStack(faults=diagnostics.faults) as resources:
        checkpointer = await resources.enter_async_context(
            open_checkpointer(
                require_url(configured_url(settings)),
                connect_timeout_seconds=settings.database.connect_timeout_seconds,
                statement_timeout_seconds=settings.database.statement_timeout_seconds,
                keepalive_idle_seconds=settings.database.keepalive_idle_seconds,
            )
        )
        builder.add_greeting()
        builder.add_history()
        builder.add_memory(builder.wired_visual())
        builder.add_visual()
        builder.add_meet_tools()
        thread_id = conversation_thread_id(config.owner_id, config.twin_id, config.meeting_id)
        responder = builder.build_responder(builder.build_agent(model, checkpointer), thread_id)
        conversation = builder.build_loop(responder)
        resources.push_async_callback(conversation.close)
        yield conversation
