import argparse
import asyncio
import inspect
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

from pydantic import AwareDatetime, TypeAdapter

from fakeyou.cli.chat import chat
from fakeyou.cli.diagnostics import check_setup, show_browser_tools, show_session_report
from fakeyou.cli.errors import FAILURE_EXIT_CODES
from fakeyou.cli.join import join_meeting
from fakeyou.cli.memory import (
    add_owner_note,
    correct_owner_note,
    forget_memory,
    ingest_recorded_meeting,
    initialize_memory,
    reembed_memory,
)
from fakeyou.provider_faults import ProviderFault
from fakeyou.settings import (
    Settings,
)

_SOURCE_AT_ARGUMENT = TypeAdapter(AwareDatetime)


def _source_at_argument(value: str) -> datetime:
    return _SOURCE_AT_ARGUMENT.validate_python(value)


def _note_id_argument(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise ValueError(f"--note-id must be a UUID: {value}") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Digital twin integration foundation", allow_abbrev=False
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "check", help="Inspect installed packages and credential presence", allow_abbrev=False
    ).set_defaults(handler=check_setup)
    commands.add_parser(
        "browser-tools",
        help="Launch CloakBrowser and list its native MCP tools",
        allow_abbrev=False,
    ).set_defaults(handler=show_browser_tools)
    commands.add_parser(
        "memory-init", help="Create the official Graphiti database indexes", allow_abbrev=False
    ).set_defaults(handler=initialize_memory)
    memory_add_parser = commands.add_parser(
        "memory-add", help="Store an owner note in the persona's memory", allow_abbrev=False
    )
    memory_add_parser.add_argument("text")
    memory_add_parser.add_argument(
        "--source-at", dest="source_at", type=_source_at_argument, default=None
    )
    memory_add_parser.add_argument(
        "--note-id", dest="note_id", type=_note_id_argument, default=None
    )
    memory_add_parser.set_defaults(handler=add_owner_note)
    memory_correct_parser = commands.add_parser(
        "memory-correct",
        help="Replace one owner note with a corrected note in the persona's memory",
        allow_abbrev=False,
    )
    memory_correct_parser.add_argument("text")
    memory_correct_parser.add_argument("--episode", dest="episode_uuid", required=True)
    memory_correct_parser.add_argument(
        "--source-at", dest="source_at", type=_source_at_argument, required=True
    )
    memory_correct_parser.add_argument(
        "--note-id", dest="note_id", type=_note_id_argument, default=None
    )
    memory_correct_parser.set_defaults(handler=correct_owner_note)
    memory_forget_parser = commands.add_parser(
        "memory-forget",
        help="Delete one meeting or one episode from the persona's memory",
        allow_abbrev=False,
    )
    forget_target = memory_forget_parser.add_mutually_exclusive_group(required=True)
    forget_target.add_argument("--meeting", dest="meeting_id", default=None)
    forget_target.add_argument("--episode", dest="episode_uuid", default=None)
    memory_forget_parser.add_argument(
        "--keep-record",
        action="store_true",
        help="Keep the meeting record, journal, and thread so the meeting can be ingested again",
    )
    memory_forget_parser.set_defaults(handler=forget_memory)
    memory_ingest_parser = commands.add_parser(
        "memory-ingest",
        help="Ingest a recorded meeting into the persona's memory, resuming",
        allow_abbrev=False,
    )
    memory_ingest_parser.add_argument("--meeting", dest="meeting_id", required=True)
    memory_ingest_parser.add_argument("--twin-name", default=None)
    memory_ingest_parser.set_defaults(handler=ingest_recorded_meeting)
    memory_reembed_parser = commands.add_parser(
        "memory-reembed",
        help="Recompute the persona's entity and fact embeddings with the configured model",
        allow_abbrev=False,
    )
    memory_reembed_parser.set_defaults(handler=reembed_memory)
    report_parser = commands.add_parser(
        "report",
        help="Summarize latency, replies, tools, ingestion, and faults of one meeting",
        allow_abbrev=False,
    )
    report_parser.add_argument("--meeting", dest="meeting_id", required=True)
    report_parser.add_argument("--journal-dir", type=Path, default=None)
    report_parser.set_defaults(handler=show_session_report)
    for operator_parser in (
        memory_add_parser,
        memory_correct_parser,
        memory_forget_parser,
        memory_ingest_parser,
        memory_reembed_parser,
    ):
        operator_parser.add_argument("--persona", dest="persona_path", type=Path, required=True)
        operator_parser.add_argument("--owner-id", dest="owner_id", default=None)
        operator_parser.add_argument("--twin-id", default=None)
    join_parser = commands.add_parser(
        "join", help="Join a Google Meet link as a guest twin", allow_abbrev=False
    )
    join_parser.add_argument("link")
    join_parser.set_defaults(handler=join_meeting)
    join_parser.add_argument("--twin-name", default=None)
    join_parser.add_argument("--owner-id", dest="owner_id", default=None)
    join_parser.add_argument("--twin-id", default=None)
    join_parser.add_argument("--meeting-id", default=None)
    join_parser.add_argument("--lobby-timeout", type=float, default=None)
    join_parser.add_argument("--duration", type=float, default=None)
    join_parser.add_argument("--profile-dir", default=None)
    join_parser.add_argument("--headless", action="store_true", default=None)
    join_parser.add_argument(
        "--audio",
        dest="audio_enabled",
        action="store_true",
        help="Enable Linux virtual audio and Muse transcription",
    )
    join_parser.add_argument(
        "--speak",
        dest="initial_speech",
        default=None,
        help="Speak this test sentence once after admission; requires --audio",
    )
    join_parser.add_argument(
        "--converse",
        dest="conversation_enabled",
        action="store_true",
        help="Enable agent replies to final transcripts; requires --audio",
    )
    join_parser.add_argument(
        "--persona",
        dest="persona_path",
        type=Path,
        default=None,
        help="Owner persona JSON profile; requires --converse",
    )
    join_parser.add_argument(
        "--brief",
        dest="brief_path",
        type=Path,
        default=None,
        help="Per-meeting brief JSON; requires --converse, --persona, and --meeting-id",
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
        "chat", help="Run one agent turn with a persistent checkpoint", allow_abbrev=False
    )
    chat_parser.add_argument("message")
    chat_parser.add_argument("--thread", required=True)
    chat_parser.add_argument(
        "--browser", dest="use_browser", action="store_true", help="Enable real browser actions"
    )
    chat_parser.add_argument(
        "--memory", dest="use_memory", action="store_true", help="Enable Graphiti recall and writes"
    )
    chat_parser.set_defaults(handler=chat)
    return parser


def main() -> None:
    options = vars(build_parser().parse_args())
    handler = options.pop("handler")
    del options["command"]
    try:
        outcome = handler(**options, settings=Settings())
        if inspect.iscoroutine(outcome):
            asyncio.run(outcome)
    except ValueError as error:
        print(f"fakeyou: error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except ProviderFault as fault:
        print(f"fakeyou: error: memory unavailable: {fault}", file=sys.stderr)
        raise SystemExit(FAILURE_EXIT_CODES["infra_failure"]) from None


if __name__ == "__main__":
    main()
