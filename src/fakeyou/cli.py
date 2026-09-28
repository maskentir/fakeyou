import argparse
import asyncio
import json
import os
import re
import signal
import sys
import uuid
from collections.abc import Iterator
from contextlib import AsyncExitStack, ExitStack, contextmanager
from functools import partial
from importlib.metadata import version
from importlib.util import find_spec
from pathlib import Path
from urllib.parse import urlparse

from langchain.chat_models import init_chat_model
from langchain_core.tools import BaseTool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pipecat.frames.frames import TranscriptionFrame

from fakeyou.agent import create_meeting_agent
from fakeyou.audio import AGENT_SINK_NAME, MEETING_MONITOR_SOURCE_NAME, open_meeting_audio
from fakeyou.browser import open_browser_tools
from fakeyou.memory import (
    Neo4jMemoryConfig,
    create_memory_tools,
    ensure_indexes,
    open_graphiti_memory,
)
from fakeyou.runtime import SessionConfig, run_meeting_session
from fakeyou.settings import Settings, require_secret


class CliError(ValueError):
    pass


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
    )


async def initialize_memory(settings: Settings) -> None:
    async with open_graphiti_memory(memory_config(settings)) as memory:
        await ensure_indexes(memory)
        print("Graphiti indexes initialized.")


async def chat(
    message: str, thread: str, use_browser: bool, use_memory: bool, settings: Settings
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", thread):
        raise CliError("--thread must be 1-128 characters using letters, numbers, '_' or '-'")
    api_key = require_secret(settings.openai_api_key, "FAKEYOU_OPENAI_API_KEY")
    model = init_chat_model(settings.chat_model, api_key=api_key, timeout=60, max_retries=2)
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

    async def transcript(frame: TranscriptionFrame) -> None:
        print(
            json.dumps(
                {
                    "event": "transcript_final",
                    "owner_id": config.owner_id,
                    "twin_id": config.twin_id,
                    "meeting_id": config.meeting_id,
                    "text": frame.text,
                    "speaker_id": frame.user_id,
                    "timestamp": frame.timestamp,
                }
            ),
            flush=True,
        )

    if audio_enabled:
        audio_factory = partial(
            open_meeting_audio,
            muse_api_key=muse_key,
            openai_api_key=tts_key,
            on_transcript=transcript,
        )

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
    with stop_signal_handlers(stop):
        result = await run_meeting_session(
            config, stop=stop, open_audio=audio_factory, initial_speech=initial_speech
        )
    print(
        json.dumps(
            {
                "meeting_id": resolved_meeting_id,
                "phase": result.phase,
                "terminal_reason": result.terminal_reason,
                "detail": result.detail,
            },
            indent=2,
        )
    )
    if result.terminal_reason == "audio_failure":
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Digital twin integration foundation")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Inspect installed packages and credential presence")
    commands.add_parser("browser-tools", help="Launch CloakBrowser and list its native MCP tools")
    commands.add_parser("memory-init", help="Create the official Graphiti database indexes")
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
    chat_parser = commands.add_parser(
        "chat", help="Run one agent turn with a persistent checkpoint"
    )
    chat_parser.add_argument("message")
    chat_parser.add_argument("--thread", required=True)
    chat_parser.add_argument("--browser", action="store_true", help="Enable real browser actions")
    chat_parser.add_argument(
        "--memory", action="store_true", help="Enable Graphiti recall and writes"
    )
    arguments = parser.parse_args()
    try:
        settings = Settings()
        if arguments.command == "check":
            check_setup(settings)
        elif arguments.command == "browser-tools":
            asyncio.run(show_browser_tools(settings))
        elif arguments.command == "memory-init":
            asyncio.run(initialize_memory(settings))
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


if __name__ == "__main__":
    main()
