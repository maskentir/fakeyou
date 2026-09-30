import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from dataclasses import asdict, replace
from functools import partial
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from graphiti_core import Graphiti
from langchain.agents.middleware import AgentMiddleware
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pipecat.frames.frames import TranscriptionFrame
from pipecat.services.meta.stt import META_STT_WS_URL
from pydantic import TypeAdapter

from fakeyou.agent import DEFAULT_PERSONA, create_meeting_agent
from fakeyou.audio import (
    AGENT_SINK_NAME,
    CAPTURE_SAMPLE_RATE,
    MEETING_MONITOR_SOURCE_NAME,
    open_meeting_audio,
)
from fakeyou.browser import open_browser_tools
from fakeyou.conversation import (
    ConversationLoop,
    ConversationPhase,
    LangGraphResponder,
    ToolResultCallback,
    conversation_thread_id,
    transcript_event,
)
from fakeyou.faults import FaultReporter
from fakeyou.ingestion import IngestResult, ingest_meeting
from fakeyou.journal import JOURNAL_BACKUP_COUNT, ProviderJournal
from fakeyou.memory import (
    Neo4jMemoryConfig,
    create_memory_tools,
    ensure_indexes,
    forget_episode,
    forget_meeting,
    open_graphiti_memory,
    recall_error_middleware,
    remember_owner_note,
    verify_memory_connection,
)
from fakeyou.persona import PersonaProfile, build_persona, load_persona, speech_keywords
from fakeyou.playback import PlaybackEvent
from fakeyou.preflight import provider_probe_hosts
from fakeyou.provider_faults import ProviderFault, classify_exception
from fakeyou.record import MeetingRecord, meeting_record_path, read_meeting_record
from fakeyou.report import build_session_report, read_journal
from fakeyou.runtime import SessionConfig, SessionResult, run_meeting_session
from fakeyou.session import SessionId
from fakeyou.settings import Settings, require_secret
from fakeyou.voice import TTS_MODEL, TTS_VOICE


class CliError(ValueError):
    pass


OPENAI_API_URL = "https://api.openai.com"
CHAT_TIMEOUT_SECONDS = 60
CHAT_MAX_RETRIES = 2


def chat_endpoint(settings: Settings) -> str:
    if settings.chat_base_url:
        return settings.chat_base_url
    return OPENAI_API_URL if settings.chat_model.startswith("openai:") else ""


def preflight_hosts(settings: Settings) -> tuple[str, ...]:
    return provider_probe_hosts(
        tts_base_url=OPENAI_API_URL,
        stt_ws_url=META_STT_WS_URL,
        agent_base_url=chat_endpoint(settings),
    )


def create_chat_model(settings: Settings) -> BaseChatModel:
    api_key = require_secret(
        settings.chat_api_key or settings.openai_api_key,
        "FAKEYOU_CHAT_API_KEY or FAKEYOU_OPENAI_API_KEY",
    )
    options: dict[str, Any] = {"timeout": CHAT_TIMEOUT_SECONDS, "max_retries": CHAT_MAX_RETRIES}
    if settings.chat_base_url:
        options["base_url"] = settings.chat_base_url
    if settings.chat_reasoning_effort:
        options["reasoning_effort"] = settings.chat_reasoning_effort
    model: BaseChatModel = init_chat_model(settings.chat_model, api_key=api_key, **options)
    return model


MEMORY_CONNECT_TIMEOUT_SECONDS = 10.0

_FAILURE_EXIT_CODES = {
    "infra_failure": 3,
    "browser_failure": 1,
    "audio_failure": 1,
    "conversation_failure": 1,
}


def check_setup(settings: Settings) -> None:
    packages = (
        "langchain",
        "langgraph",
        "langchain-mcp-adapters",
        "mcp",
        "cloakbrowser",
        "pipecat-ai",
        "graphiti-core",
    )
    print(
        json.dumps(
            {
                "packages": {name: version(name) for name in packages},
                "playwright_mcp_installed": Path("node_modules/@playwright/mcp/cli.js").is_file(),
                "openai_key_configured": bool(
                    settings.openai_api_key and settings.openai_api_key.get_secret_value().strip()
                ),
                "model_key_configured": bool(
                    settings.model_api_key and settings.model_api_key.get_secret_value().strip()
                ),
                "chat_model": settings.chat_model,
                "models": {
                    "reply": {
                        "model": settings.chat_model,
                        "base_url": settings.chat_base_url,
                        "reasoning_effort": settings.chat_reasoning_effort,
                    },
                    "memory": {
                        "llm": settings.memory_llm_model,
                        "small": settings.memory_small_model,
                        "embedding": settings.memory_embedding_model,
                    },
                    "stt": {
                        "model": settings.stt_model,
                        "mode": "ENDPOINTING",
                        "sample_rate": CAPTURE_SAMPLE_RATE,
                    },
                    "tts": {"model": TTS_MODEL, "voice": TTS_VOICE},
                },
                "live_services_verified": False,
            },
            indent=2,
        )
    )


async def show_browser_tools(settings: Settings) -> None:
    async with open_browser_tools(
        headless=settings.headless,
        license_key=settings.cloakbrowser_license_key,
    ) as tools:
        print("\n".join(tool.name for tool in tools))


def memory_config(settings: Settings) -> Neo4jMemoryConfig:
    return Neo4jMemoryConfig(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=require_secret(settings.neo4j_password, "FAKEYOU_NEO4J_PASSWORD"),
        api_key=require_secret(settings.openai_api_key, "FAKEYOU_OPENAI_API_KEY"),
        llm_model=settings.memory_llm_model,
        small_model=settings.memory_small_model,
        embedding_model=settings.memory_embedding_model,
    )


async def initialize_memory(settings: Settings) -> None:
    async with open_graphiti_memory(memory_config(settings)) as memory:
        await ensure_indexes(memory)
        print("Graphiti indexes initialized.")


@asynccontextmanager
async def open_persona_memory(settings: Settings) -> AsyncIterator[Graphiti]:
    async with open_graphiti_memory(memory_config(settings)) as memory:
        try:
            async with asyncio.timeout(MEMORY_CONNECT_TIMEOUT_SECONDS):
                await verify_memory_connection(memory)
        except Exception as error:
            raise classify_exception("memory", error) from error
        yield memory


async def load_operator_persona(
    persona_path: Path, owner_id: str | None, twin_id: str | None, settings: Settings
) -> PersonaProfile:
    try:
        return await load_persona(
            persona_path,
            owner_id=owner_id or settings.owner_id,
            twin_id=twin_id or settings.twin_id,
        )
    except OSError as error:
        raise CliError(f"--persona cannot be read: {error.strerror}") from None


async def add_owner_note(
    text: str,
    *,
    persona_path: Path,
    owner_id: str | None,
    twin_id: str | None,
    settings: Settings,
) -> None:
    profile = await load_operator_persona(persona_path, owner_id, twin_id, settings)
    if not text.strip():
        raise CliError("note text must not be blank")
    async with open_persona_memory(settings) as memory:
        result = await remember_owner_note(memory, group_id=profile.memory_group, text=text)
    print(
        json.dumps(
            {
                "episode_uuid": result.episode.uuid,
                "facts": [edge.fact for edge in result.edges],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if not result.edges:
        print(
            "fakeyou: warning: no facts were extracted, so recall cannot find this note; "
            "rephrase it with the named people, projects, or dates it relates to",
            file=sys.stderr,
        )


async def ingest_until_stopped(
    memory: Graphiti,
    record_path: Path,
    *,
    profile: PersonaProfile,
    meeting_id: str,
    twin_name: str,
    journal: ProviderJournal,
    faults: FaultReporter,
) -> IngestResult:
    stop = asyncio.Event()
    with stop_signal_handlers(stop):
        return await ingest_meeting(
            memory,
            read_meeting_record(record_path),
            group_id=profile.memory_group,
            meeting_id=meeting_id,
            twin_name=twin_name,
            vocabulary=profile.vocabulary,
            journal=journal,
            faults=faults,
            stop=stop,
        )


async def ingest_recorded_meeting(
    *,
    meeting_id: str,
    persona_path: Path,
    owner_id: str | None,
    twin_id: str | None,
    twin_name: str | None,
    settings: Settings,
) -> None:
    TypeAdapter(SessionId).validate_python(meeting_id)
    profile = await load_operator_persona(persona_path, owner_id, twin_id, settings)
    record_path = meeting_record_path(settings.meeting_record_dir, meeting_id)
    if not record_path.exists():
        raise CliError(f"no meeting record for {meeting_id} at {record_path}")
    journal = ProviderJournal(meeting_id, path=settings.journal_dir / f"{meeting_id}.jsonl")
    try:
        async with open_persona_memory(settings) as memory:
            result = await ingest_until_stopped(
                memory,
                record_path,
                profile=profile,
                meeting_id=meeting_id,
                twin_name=twin_name or profile.display_name,
                journal=journal,
                faults=FaultReporter(meeting_id=meeting_id),
            )
    finally:
        journal.close()
    print(json.dumps({"meeting_id": meeting_id, **asdict(result)}))
    if not result.completed:
        raise SystemExit(1)


async def forget_memory(
    *,
    persona_path: Path,
    owner_id: str | None,
    twin_id: str | None,
    meeting_id: str | None,
    episode_uuid: str | None,
    settings: Settings,
    keep_record: bool = False,
) -> None:
    if (meeting_id is None) == (episode_uuid is None):
        raise CliError("memory-forget requires exactly one of --meeting or --episode")
    if keep_record and meeting_id is None:
        raise CliError("--keep-record only applies to --meeting")
    if meeting_id is not None:
        TypeAdapter(SessionId).validate_python(meeting_id)
    profile = await load_operator_persona(persona_path, owner_id, twin_id, settings)
    async with open_persona_memory(settings) as memory:
        if meeting_id is not None:
            removed = await forget_meeting(
                memory, group_id=profile.memory_group, meeting_id=meeting_id
            )
        elif episode_uuid is not None:
            await forget_episode(memory, group_id=profile.memory_group, episode_uuid=episode_uuid)
            removed = 1
    summary: dict[str, Any] = {"removed_episodes": removed}
    if meeting_id is not None and not keep_record:
        summary |= await forget_meeting_files(settings, profile, meeting_id)
    print(json.dumps(summary))


async def forget_meeting_files(
    settings: Settings, profile: PersonaProfile, meeting_id: str
) -> dict[str, Any]:
    record_dir = meeting_record_path(settings.meeting_record_dir, meeting_id).parent
    record_removed = record_dir.exists()
    shutil.rmtree(record_dir, ignore_errors=True)
    journal_path = settings.journal_dir / f"{meeting_id}.jsonl"
    journal_files = [journal_path] + [
        Path(f"{journal_path}.{index}") for index in range(1, JOURNAL_BACKUP_COUNT + 1)
    ]
    removed_journals = [path for path in journal_files if path.exists()]
    for path in removed_journals:
        path.unlink()
    thread_removed = settings.checkpoint_path.exists()
    if thread_removed:
        async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
            await saver.adelete_thread(
                conversation_thread_id(profile.owner_id, profile.twin_id, meeting_id)
            )
    return {
        "record_removed": record_removed,
        "journal_files_removed": len(removed_journals),
        "conversation_thread_removed": thread_removed,
    }


def show_session_report(meeting_id: str, journal_dir: Path | None, settings: Settings) -> None:
    TypeAdapter(SessionId).validate_python(meeting_id)
    journal_path = (journal_dir or settings.journal_dir) / f"{meeting_id}.jsonl"
    if not journal_path.exists():
        raise CliError(f"no journal for meeting {meeting_id} at {journal_path}")
    report = build_session_report(read_journal(journal_path))
    print(json.dumps({"meeting_id": meeting_id, **report}, ensure_ascii=False, indent=2))


async def chat(
    message: str, thread: str, use_browser: bool, use_memory: bool, settings: Settings
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", thread):
        raise CliError("--thread must be 1-128 characters using letters, numbers, '_' or '-'")
    model = create_chat_model(settings)
    settings.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncExitStack() as resources:
        checkpointer = await resources.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path))
        )
        tools: list[BaseTool] = []
        if use_memory:
            memory = await resources.enter_async_context(
                open_graphiti_memory(memory_config(settings))
            )
            tools.extend(create_memory_tools(memory, group_id=settings.memory_group))
        if use_browser:
            tools.extend(
                await resources.enter_async_context(
                    open_browser_tools(
                        headless=settings.headless,
                        license_key=settings.cloakbrowser_license_key,
                    )
                )
            )
        agent = create_meeting_agent(model, tools=tools, checkpointer=checkpointer)
        response = await agent.ainvoke(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": thread}, "recursion_limit": 30},
        )
        print(response["messages"][-1].content)


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


async def join_meeting(
    link: str,
    *,
    twin_name: str | None,
    owner_id: str | None,
    twin_id: str | None,
    meeting_id: str | None,
    lobby_timeout: float | None,
    duration: float | None,
    profile_dir: str | None,
    settings: Settings,
    audio_enabled: bool = False,
    initial_speech: str | None = None,
    headless: bool | None = None,
    memory: bool = False,
    conversation_enabled: bool = False,
    wake_phrase: str | None = None,
    barge_in: bool = False,
    persona_path: Path | None = None,
) -> None:
    parsed_link = urlparse(link)
    if (
        parsed_link.scheme != "https"
        or parsed_link.hostname != "meet.google.com"
        or parsed_link.path.strip("/") == ""
    ):
        raise CliError("link must be an https://meet.google.com/ meeting URL")
    if lobby_timeout is not None and not lobby_timeout > 0:
        raise CliError("--lobby-timeout must be positive")
    if duration is not None and not duration > 0:
        raise CliError("--duration must be positive")
    if initial_speech is not None and (not audio_enabled or not initial_speech.strip()):
        raise CliError("--speak requires --audio and nonempty text")
    if conversation_enabled and not audio_enabled:
        raise CliError("--converse requires --audio")
    if memory and not conversation_enabled:
        raise CliError("--memory requires --converse")
    if persona_path is not None and not conversation_enabled:
        raise CliError("--persona requires --converse")
    if memory and persona_path is None:
        raise CliError("--memory requires --persona")
    if wake_phrase and not conversation_enabled:
        raise CliError("--wake-phrase requires --converse")
    if barge_in and not conversation_enabled:
        raise CliError("--barge-in requires --converse")
    audio_factory = None
    if audio_enabled:
        if (
            sys.platform != "linux"
            or os.environ.get("PULSE_SOURCE") != MEETING_MONITOR_SOURCE_NAME
            or os.environ.get("PULSE_SINK") != AGENT_SINK_NAME
            or find_spec("pyaudio") is None
        ):
            raise CliError(
                "--audio requires the Linux audio worker; use docker compose run meeting-worker"
            )
        muse_key = require_secret(settings.model_api_key, "FAKEYOU_MODEL_API_KEY")
        tts_key = require_secret(settings.openai_api_key, "FAKEYOU_OPENAI_API_KEY")
    stop = asyncio.Event()
    resolved_meeting_id = meeting_id or uuid.uuid4().hex[:12]
    resolved_twin_id = twin_id or settings.twin_id
    resolved_headless = settings.headless if headless is None else headless
    config = SessionConfig(
        owner_id=owner_id or settings.owner_id,
        twin_id=resolved_twin_id,
        meeting_id=resolved_meeting_id,
        link=link,
        twin_name=twin_name or settings.twin_name,
        browser_profile_dir=Path(profile_dir)
        if profile_dir
        else Path(".runtime/profiles") / resolved_twin_id,
        lobby_timeout_seconds=lobby_timeout
        if lobby_timeout is not None
        else settings.lobby_timeout_seconds,
        session_duration_seconds=duration
        if duration is not None
        else settings.session_duration_seconds,
        headless=resolved_headless,
        license_key=settings.cloakbrowser_license_key,
    )
    profile: PersonaProfile | None = None
    if persona_path is not None:
        profile = await load_operator_persona(
            persona_path, config.owner_id, config.twin_id, settings
        )
        if memory and not (profile.allowed_tools or profile.ingest_transcripts):
            raise CliError(
                "--memory requires recall_meeting_context or ingest_transcripts in the persona"
            )
        if not twin_name:
            config = replace(config, twin_name=profile.display_name)
    faults = FaultReporter(meeting_id=resolved_meeting_id)
    journal = ProviderJournal(
        resolved_meeting_id, path=settings.journal_dir / f"{resolved_meeting_id}.jsonl"
    )
    record = MeetingRecord(
        meeting_record_path(settings.meeting_record_dir, resolved_meeting_id), faults=faults
    )

    conversation: ConversationLoop | None = None
    graphiti: Graphiti | None = None
    result: SessionResult | None = None
    async with AsyncExitStack() as resources:
        resources.callback(journal.close)
        resources.callback(record.close)
        if memory and profile is not None:
            try:
                graphiti = await resources.enter_async_context(open_persona_memory(settings))
            except ProviderFault as fault:
                faults.record(fault)
                result = SessionResult(
                    "ended", "infra_failure", f"memory unavailable: {fault}", faults.report()
                )

        def log_transition(
            from_phase: ConversationPhase, to_phase: ConversationPhase, detail: str
        ) -> None:
            journal.record(
                "conversation_transition",
                source="conversation",
                from_phase=from_phase,
                to_phase=to_phase,
                detail=detail,
            )

        def log_playback(event: PlaybackEvent) -> None:
            journal.record(event.name, source="playback", text=event.text, detail=event.detail)
            if event.name == "speak_finished":
                record.add_twin_speech(event.text)

        if conversation_enabled and result is None:
            conversation = await resources.enter_async_context(
                open_conversation(
                    settings,
                    config,
                    graphiti=graphiti,
                    profile=profile,
                    on_tool_result=record.add_tool_result,
                    on_transition=log_transition,
                    wake_phrases=[phrase.strip() for phrase in (wake_phrase or "").split(",")],
                    barge_in=barge_in,
                    faults=faults,
                    journal=journal,
                )
            )

        async def transcript(frame: TranscriptionFrame) -> None:
            event = transcript_event(frame)
            journal.record(
                "transcript_final",
                source=event.source,
                transcript_id=event.id,
                text=event.text,
                participant=event.participant,
                spoken_at=event.timestamp,
                finalized=event.finalized,
            )
            record.add_transcript(event)
            if conversation is not None:
                await conversation.handle_transcript(event)

        if audio_enabled:
            audio_factory = partial(
                open_meeting_audio,
                muse_api_key=muse_key,
                openai_api_key=tts_key,
                on_transcript=transcript,
                on_playback_event=log_playback,
                fault_reporter=faults,
                journal=journal,
                deaf_window_seconds=config.deaf_window_seconds,
                deaf_grace_seconds=config.deaf_grace_seconds,
                stt_model=settings.stt_model,
                stt_keywords=speech_keywords(profile, config.twin_name) if profile else None,
            )
        preflight_kwargs: dict[str, Any] = (
            {
                "preflight_hosts": preflight_hosts(settings),
                "preflight_budget_seconds": config.preflight_budget_seconds,
            }
            if audio_enabled
            else {}
        )
        if result is None:
            with stop_signal_handlers(stop):
                result = await run_meeting_session(
                    config,
                    stop=stop,
                    open_audio=audio_factory,
                    initial_speech=initial_speech,
                    conversation=conversation,
                    faults=faults,
                    journal=journal,
                    **preflight_kwargs,
                )
        exit_code = (
            1
            if result.terminal_reason is None
            else _FAILURE_EXIT_CODES.get(result.terminal_reason, 0)
        )
        journal.record(
            "session_result",
            phase=result.phase,
            terminal_reason=result.terminal_reason,
            exit_code=exit_code,
        )
        if graphiti is not None and profile is not None and profile.ingest_transcripts:
            try:
                await ingest_until_stopped(
                    graphiti,
                    meeting_record_path(settings.meeting_record_dir, resolved_meeting_id),
                    profile=profile,
                    meeting_id=resolved_meeting_id,
                    twin_name=config.twin_name,
                    journal=journal,
                    faults=faults,
                )
            except Exception as error:
                faults.record(classify_exception("memory_ingest", error))
    print(
        json.dumps(
            {
                "meeting_id": resolved_meeting_id,
                "phase": result.phase,
                "terminal_reason": result.terminal_reason,
                "detail": result.detail,
                "faults": getattr(result, "faults", None),
            },
            indent=2,
        )
    )
    print(
        json.dumps(
            {
                "event": "cli_exit",
                "meeting_id": resolved_meeting_id,
                "terminal_reason": result.terminal_reason,
                "exit_code": exit_code,
            }
        ),
        file=sys.stderr,
        flush=True,
    )
    if exit_code != 0:
        raise SystemExit(exit_code)


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
    journal: ProviderJournal | None = None,
    on_tool_result: ToolResultCallback | None = None,
) -> AsyncIterator[ConversationLoop]:
    model = create_chat_model(settings)
    settings.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncExitStack() as resources:
        checkpointer = await resources.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path))
        )
        tools: list[BaseTool] = []
        middleware: list[AgentMiddleware[Any, Any, Any]] = []
        if graphiti is not None and profile is not None and profile.allowed_tools:
            tools.extend(
                tool
                for tool in create_memory_tools(graphiti, group_id=profile.memory_group)
                if tool.name in profile.allowed_tools
            )
            middleware.append(recall_error_middleware(faults))
        agent = create_meeting_agent(
            model,
            tools=tools,
            checkpointer=checkpointer,
            persona=build_persona(profile) if profile is not None else DEFAULT_PERSONA,
            middleware=middleware,
        )
        conversation = ConversationLoop(
            LangGraphResponder(
                agent,
                conversation_thread_id(config.owner_id, config.twin_id, config.meeting_id),
                journal=journal,
                on_tool_result=on_tool_result,
            ),
            on_transition=on_transition,
            wake_phrases=wake_phrases,
            barge_in=barge_in,
            faults=faults,
            journal=journal,
        )
        resources.push_async_callback(conversation.close)
        yield conversation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Digital twin integration foundation")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Inspect installed packages and credential presence")
    commands.add_parser("browser-tools", help="Launch CloakBrowser and list its native MCP tools")
    commands.add_parser("memory-init", help="Create the official Graphiti database indexes")
    memory_add_parser = commands.add_parser(
        "memory-add", help="Store an owner note in the persona's memory"
    )
    memory_add_parser.add_argument("text")
    memory_forget_parser = commands.add_parser(
        "memory-forget", help="Delete one meeting or one episode from the persona's memory"
    )
    forget_target = memory_forget_parser.add_mutually_exclusive_group(required=True)
    forget_target.add_argument("--meeting", dest="meeting_id", default=None)
    forget_target.add_argument("--episode", dest="episode_uuid", default=None)
    memory_forget_parser.add_argument(
        "--keep-record",
        action="store_true",
        help="Keep the meeting record, journal, and thread so the meeting can be ingested again",
    )
    memory_ingest_parser = commands.add_parser(
        "memory-ingest", help="Ingest a recorded meeting into the persona's memory, resuming"
    )
    memory_ingest_parser.add_argument("--meeting", dest="meeting_id", required=True)
    memory_ingest_parser.add_argument("--twin-name", default=None)
    report_parser = commands.add_parser(
        "report", help="Summarize latency, replies, tools, ingestion, and faults of one meeting"
    )
    report_parser.add_argument("--meeting", dest="meeting_id", required=True)
    report_parser.add_argument("--journal-dir", type=Path, default=None)
    for operator_parser in (memory_add_parser, memory_forget_parser, memory_ingest_parser):
        operator_parser.add_argument("--persona", type=Path, required=True)
        operator_parser.add_argument("--owner-id", "--owner", dest="owner_id", default=None)
        operator_parser.add_argument("--twin-id", default=None)
    join_parser = commands.add_parser("join", help="Join a Google Meet link as a guest twin")
    join_parser.add_argument("link")
    join_parser.add_argument("--twin-name", default=None)
    join_parser.add_argument("--owner-id", "--owner", dest="owner_id", default=None)
    join_parser.add_argument("--twin-id", default=None)
    join_parser.add_argument("--meeting-id", default=None)
    join_parser.add_argument("--lobby-timeout", type=float, default=None)
    join_parser.add_argument("--duration", type=float, default=None)
    join_parser.add_argument("--profile-dir", default=None)
    join_parser.add_argument("--headless", action="store_true", default=None)
    join_parser.add_argument(
        "--audio", action="store_true", help="Enable Linux virtual audio and Muse transcription"
    )
    join_parser.add_argument(
        "--speak",
        default=None,
        help="Speak this test sentence once after admission; requires --audio",
    )
    join_parser.add_argument(
        "--converse",
        action="store_true",
        help="Enable agent replies to final transcripts; requires --audio",
    )
    join_parser.add_argument(
        "--persona",
        type=Path,
        default=None,
        help="Owner persona JSON profile; requires --converse",
    )
    join_parser.add_argument(
        "--memory",
        action="store_true",
        help="Enable persona-scoped Graphiti recall; requires --converse and --persona",
    )
    join_parser.add_argument(
        "--wake-phrase",
        default=None,
        help="Comma-separated phrases that address the twin; requires --converse",
    )
    join_parser.add_argument(
        "--barge-in",
        action="store_true",
        help="Let new speech interrupt playback; requires --converse",
    )
    chat_parser = commands.add_parser(
        "chat", help="Run one agent turn with a persistent checkpoint"
    )
    chat_parser.add_argument("message")
    chat_parser.add_argument("--thread", required=True)
    chat_parser.add_argument("--browser", action="store_true", help="Enable real browser actions")
    chat_parser.add_argument(
        "--memory", action="store_true", help="Enable Graphiti recall and writes"
    )
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    try:
        settings = Settings()
        if arguments.command == "check":
            check_setup(settings)
        elif arguments.command == "browser-tools":
            asyncio.run(show_browser_tools(settings))
        elif arguments.command == "memory-init":
            asyncio.run(initialize_memory(settings))
        elif arguments.command == "report":
            show_session_report(arguments.meeting_id, arguments.journal_dir, settings)
        elif arguments.command == "memory-add":
            asyncio.run(
                add_owner_note(
                    arguments.text,
                    persona_path=arguments.persona,
                    owner_id=arguments.owner_id,
                    twin_id=arguments.twin_id,
                    settings=settings,
                )
            )
        elif arguments.command == "memory-forget":
            asyncio.run(
                forget_memory(
                    persona_path=arguments.persona,
                    owner_id=arguments.owner_id,
                    twin_id=arguments.twin_id,
                    meeting_id=arguments.meeting_id,
                    episode_uuid=arguments.episode_uuid,
                    settings=settings,
                    keep_record=arguments.keep_record,
                )
            )
        elif arguments.command == "memory-ingest":
            asyncio.run(
                ingest_recorded_meeting(
                    meeting_id=arguments.meeting_id,
                    persona_path=arguments.persona,
                    owner_id=arguments.owner_id,
                    twin_id=arguments.twin_id,
                    twin_name=arguments.twin_name,
                    settings=settings,
                )
            )
        elif arguments.command == "join":
            asyncio.run(
                join_meeting(
                    arguments.link,
                    twin_name=arguments.twin_name,
                    owner_id=arguments.owner_id,
                    twin_id=arguments.twin_id,
                    meeting_id=arguments.meeting_id,
                    lobby_timeout=arguments.lobby_timeout,
                    duration=arguments.duration,
                    profile_dir=arguments.profile_dir,
                    settings=settings,
                    audio_enabled=arguments.audio,
                    initial_speech=arguments.speak,
                    headless=arguments.headless,
                    memory=arguments.memory,
                    conversation_enabled=arguments.converse,
                    wake_phrase=arguments.wake_phrase,
                    barge_in=arguments.barge_in,
                    persona_path=arguments.persona,
                )
            )
        else:
            asyncio.run(
                chat(
                    arguments.message,
                    arguments.thread,
                    arguments.browser,
                    arguments.memory,
                    settings,
                )
            )
    except ValueError as error:
        print(f"fakeyou: error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except ProviderFault as fault:
        print(f"fakeyou: error: memory unavailable: {fault}", file=sys.stderr)
        raise SystemExit(_FAILURE_EXIT_CODES["infra_failure"]) from None


if __name__ == "__main__":
    main()
