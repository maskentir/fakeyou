import asyncio
import signal
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import (
    AsyncExitStack,
    ExitStack,
    asynccontextmanager,
    contextmanager,
)
from dataclasses import dataclass, field
from typing import Any

from graphiti_core import Graphiti
from langchain.agents.middleware import AgentMiddleware, ModelFallbackMiddleware
from langchain_core.tools import BaseTool

from fakeyou.agent import (
    ENDING_RULE,
    MEETING_GUIDANCE,
    SPOKEN_LANGUAGE_GUIDANCE,
    VISUAL_GUIDANCE,
    create_meeting_agent,
    create_visual_tools,
)
from fakeyou.audio import TurnAudioTap
from fakeyou.brief import MeetingBrief
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.conversation import (
    ConversationLoop,
    ConversationPhase,
    ConversationResponder,
    LangGraphResponder,
    StreamingLangGraphResponder,
    ToolResultCallback,
    conversation_thread_id,
)
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.db.engine import configured_url, open_database, require_url
from fakeyou.db.schema import require_current
from fakeyou.db.voiceprint_store import VoiceprintRepository, VoiceprintStore
from fakeyou.faults import FaultReporter
from fakeyou.greetings import create_greeting_middleware
from fakeyou.journal import Journal
from fakeyou.memory import (
    VisualCaptionResolver,
    create_meet_action_tools,
    create_memory_prefetch_middleware,
    create_memory_tools,
    create_recall_error_middleware,
)
from fakeyou.persona import (
    PersonaProfile,
    PersonaTool,
    build_system_prompt,
    meeting_tool_names,
    memory_wiring,
    refuse_meet_tool,
)
from fakeyou.providers import (
    create_chat_model,
    create_fallback_chat_model,
    create_visual_chat_model,
)
from fakeyou.runtime import MeetActionDispatcher
from fakeyou.session import SessionConfig
from fakeyou.settings import (
    Settings,
)
from fakeyou.verify import CalibrationArtifact, load_calibration_artifact
from fakeyou.visual_caption import VisualCaptioner
from fakeyou.visual_hydrate import create_visual_hydrate_middleware, create_visual_resolver
from fakeyou.visual_store import VisualStore
from fakeyou.voiceprint_embed import SpeakerEmbedder, load_embedder
from fakeyou.voiceprint_match import VoiceprintMatcher


@dataclass(slots=True)
class VoiceprintLive:
    tap: TurnAudioTap
    matcher: VoiceprintMatcher
    embedder: SpeakerEmbedder
    min_seconds: float
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
        embedder = _load_voiceprint_embedder(settings)
        artifact = _load_voiceprint_artifact(settings)
        yield build_voiceprint_live(
            settings, store=VoiceprintStore(engine), embedder=embedder, artifact=artifact
        )


def _load_voiceprint_embedder(settings: Settings) -> SpeakerEmbedder:
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


@contextmanager
def stop_signal_handlers(stop: asyncio.Event) -> Iterator[None]:
    loop = asyncio.get_running_loop()
    with ExitStack() as handlers:
        for candidate in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(candidate)
            try:
                loop.add_signal_handler(candidate, stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            handlers.callback(signal.signal, candidate, previous)
            handlers.callback(loop.remove_signal_handler, candidate)
        yield


@asynccontextmanager
async def open_conversation(
    settings: Settings,
    config: SessionConfig,
    *,
    graphiti: Graphiti | None = None,
    profile: PersonaProfile | None = None,
    on_transition: Callable[[ConversationPhase, ConversationPhase, str], None],
    wake_phrases: Sequence[str] = (),
    barge_in: bool = False,
    faults: FaultReporter | None = None,
    journal: Journal | None = None,
    on_tool_result: ToolResultCallback | None = None,
    brief: MeetingBrief | None = None,
    meet_dispatcher: MeetActionDispatcher | None = None,
    visual_store: VisualStore | None = None,
    visual_captioner: VisualCaptioner | None = None,
) -> AsyncIterator[ConversationLoop]:
    tool_names: frozenset[PersonaTool] = frozenset()
    visual_wired = (
        settings.visual.enabled and visual_store is not None and visual_captioner is not None
    )
    if profile is None:
        if brief is not None:
            raise FakeYouError(ErrorCode.MEETING_BRIEF_REQUIRES_PROFILE)
        persona = default_persona(settings)
    else:
        tool_names = meeting_tool_names(profile, brief, memory_requested=graphiti is not None)
        persona = build_system_prompt(
            profile.model_copy(update={"allowed_tools": tool_names}),
            brief=brief,
            recall_strategy=settings.memory.recall.strategy,
        )
    if visual_wired:
        persona = f"{persona}\n\n{VISUAL_GUIDANCE}"
    model = create_chat_model(settings)
    async with AsyncExitStack() as resources:
        checkpointer = await resources.enter_async_context(
            open_checkpointer(require_url(configured_url(settings)))
        )
        tools: list[BaseTool] = []
        middleware: list[AgentMiddleware[Any, Any, Any]] = []
        greeting_language = profile.language if profile is not None else settings.speech.language
        middleware.append(create_greeting_middleware(greeting_language))
        fallback = create_fallback_chat_model(settings)
        if fallback is not None:
            middleware.append(ModelFallbackMiddleware(fallback))
        visual: VisualCaptionResolver | None = None
        if visual_wired and visual_store is not None and visual_captioner is not None:
            visual = create_visual_resolver(
                visual_store, visual_captioner, enabled=settings.visual.enabled
            )
        if graphiti is not None and profile is not None and tool_names:
            bound, prefetch = memory_wiring(tool_names, settings.memory.recall.strategy)
            if prefetch:
                middleware.append(
                    create_memory_prefetch_middleware(
                        graphiti,
                        group_id=profile.memory_group,
                        recall=settings.memory.recall,
                        faults=faults,
                        journal=journal,
                        visual_caption=visual,
                    )
                )
            if bound:
                tools.extend(
                    tool
                    for tool in create_memory_tools(
                        graphiti, group_id=profile.memory_group, recall=settings.memory.recall
                    )
                    if tool.name in bound
                )
                middleware.append(create_recall_error_middleware(faults))
        if visual_wired and visual_store is not None and visual_captioner is not None:
            middleware.append(
                create_visual_hydrate_middleware(
                    visual_store,
                    visual_captioner,
                    max_images=settings.visual.max_images,
                    journal=journal,
                    enabled=settings.visual.enabled,
                )
            )
            tools.extend(create_visual_tools(visual_store, visual_captioner))
        if profile is not None and meet_dispatcher is not None:
            meet = tool_names - {"recall_meeting_context"}
            if meet:
                tools.extend(
                    tool
                    for tool in create_meet_action_tools(
                        meet_dispatcher, refuse=refuse_meet_tool, journal=journal
                    )
                    if tool.name in meet
                )
        agent = create_meeting_agent(
            model,
            tools=tools,
            checkpointer=checkpointer,
            persona=persona,
            middleware=middleware,
        )
        thread_id = conversation_thread_id(config.owner_id, config.twin_id, config.meeting_id)
        responder: ConversationResponder
        if settings.conversation.sentence_streaming:
            responder = StreamingLangGraphResponder(
                agent,
                thread_id,
                journal=journal,
                on_tool_result=on_tool_result,
                language=settings.speech.language,
                max_sentences=settings.conversation.max_spoken_sentences,
            )
        else:
            responder = LangGraphResponder(
                agent,
                thread_id,
                journal=journal,
                on_tool_result=on_tool_result,
            )
        conversation = ConversationLoop(
            responder,
            on_transition=on_transition,
            wake_phrases=wake_phrases,
            barge_in=barge_in,
            response_timeout_seconds=settings.conversation.response_timeout_seconds,
            activation_seconds=settings.conversation.activation_seconds,
            barge_in_min_words=settings.conversation.barge_in_min_words,
            max_spoken_sentences=settings.conversation.max_spoken_sentences,
            language=settings.speech.language,
            sentence_streaming=settings.conversation.sentence_streaming,
            faults=faults,
            journal=journal,
            participant_policy_enabled=settings.conversation.participant_policy_enabled,
            pre_generate_enabled=settings.conversation.pre_generate_enabled,
            pregen_fuzzy_restart_enabled=settings.conversation.pregen_fuzzy_restart_enabled,
        )
        resources.push_async_callback(conversation.close)
        yield conversation
