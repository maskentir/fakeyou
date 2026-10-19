import asyncio
import hashlib
import json
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, override
from unittest.mock import patch

from graphiti_core import Graphiti
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

import fakeyou.orchestration.assembly as assembly
from evals._shared import sanitized_role
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.conversation import (
    LangGraphResponder,
    StreamTrace,
    ToolResultCallback,
    stream_sentences,
)
from fakeyou.conversation.meeting_messages import request_messages
from fakeyou.conversation.session import SessionConfig
from fakeyou.foundation.digest import canonical_json_digest
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.memory.persona import PersonaProfile
from fakeyou.persistence.brief import MeetingBrief

ROOT = Path(__file__).resolve().parent.parent
DEBRIEF_SOURCES = frozenset(
    {
        "src/fakeyou/debrief",
        "src/fakeyou/api/debrief.py",
        "src/fakeyou/cli/debrief.py",
        "src/fakeyou/worker/debrief.py",
    }
)
PACKAGE_SUFFIXES = frozenset({".py", ".json", ".js"})
EVAL_FILES = (
    "evals/_brief.py",
    "evals/_model_gate.py",
    "evals/_prompt_ab.py",
    "evals/_replay.py",
    "evals/_shared.py",
    "evals/_trust.py",
    "evals/_wave_report.py",
    "evals/runtime_evidence.py",
    "evals/test_brief_artifacts_eval.py",
    "evals/test_brief_gate_eval.py",
    "evals/test_brief_replay_eval.py",
    "evals/test_model_gate_eval.py",
    "evals/test_prompt_ab_eval.py",
    "evals/test_replay_eval.py",
    "evals/test_trust_artifacts_eval.py",
    "evals/test_trust_gate_eval.py",
    "evals/test_trust_native_eval.py",
    "evals/test_trust_provenance_eval.py",
    "evals/test_trust_recall_eval.py",
    "evals/test_trust_replay_eval.py",
    "evals/test_wave_report_eval.py",
    "evals/fixtures/brief-scenarios.json",
    "evals/fixtures/trust-scenarios.json",
    "evals/fixtures/wave-report-template.md",
    "examples/persona.example.json",
    "tests/_audio_fakes.py",
    "tests/_conversation_fakes.py",
    "tests/_fakes_common.py",
    "tests/_meet_browser_fakes.py",
    "tests/_memory_fakes.py",
    "tests/_persistence_fakes.py",
    "tests/_runtime_fakes.py",
    "tests/_visual_fakes.py",
    "tests/_voiceprint_fakes.py",
    "pyproject.toml",
    "uv.lock",
)
EVAL_CANDIDATE_SCHEMA = "eval_candidate/v1"


def _package_files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in (root / "src" / "fakeyou").rglob("*")
        if path.is_file() and path.suffix in PACKAGE_SUFFIXES
    )


def _in_debrief_scope(name: str) -> bool:
    return any(name == item or name.startswith(f"{item}/") for item in DEBRIEF_SOURCES)


def candidate_files(root: Path = ROOT) -> tuple[str, ...]:
    runtime = tuple(name for name in _package_files(root) if not _in_debrief_scope(name))
    return (*runtime, *EVAL_FILES)


def debrief_files(root: Path = ROOT) -> tuple[str, ...]:
    return tuple(name for name in _package_files(root) if _in_debrief_scope(name))


def _file_hashes(root: Path, paths: list[str]) -> dict[str, str | None]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        if (root / name).is_file()
        else None
        for name in paths
    }


def _digest(hashes: dict[str, str | None]) -> str:
    return canonical_json_digest(hashes, compact=True)


def capture_candidate(fixture_path: Path, *, root: Path = ROOT) -> dict[str, Any]:
    fixture_name = fixture_path.resolve().relative_to(root.resolve()).as_posix()
    paths = sorted(set(candidate_files(root)) | {fixture_name})
    hashes = _file_hashes(root, paths)
    commit = "unknown"
    dirty = True
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            commit = result.stdout.strip()
        result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            dirty = bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "schema": EVAL_CANDIDATE_SCHEMA,
        "started_at": datetime.now(UTC).isoformat(),
        "source_commit": commit,
        "dirty_at_start": dirty,
        "start_sha256": _digest(hashes),
        "start_files": hashes,
        "fixture_sha256": hashes[fixture_name],
        "scope": "fixed A/runtime/eval allowlist; excludes debrief B, private data and .env",
    }


def finish_candidate(candidate: dict[str, Any], *, root: Path = ROOT) -> dict[str, Any]:
    names = sorted(set(candidate["start_files"]) | set(candidate_files(root)))
    hashes = _file_hashes(root, names)
    changed = [name for name in names if candidate["start_files"].get(name) != hashes[name]]
    return {
        **candidate,
        "finished_at": datetime.now(UTC).isoformat(),
        "end_sha256": _digest(hashes),
        "end_files": hashes,
        "unchanged": not changed,
        "changed_files": changed,
    }


def runtime_metadata(settings: Settings) -> dict[str, Any]:
    dumped = settings.model_dump(mode="json")
    return {
        "proof_scope": (
            "native sentence-streaming responder; not full turn graph/audio"
            if settings.conversation.sentence_streaming
            else "native non-streaming agent/responder; not full turn graph/audio"
        ),
        "assembly": "fakeyou.orchestration.assembly.open_conversation",
        "boundaries": "supplied model/fallback, synthetic Graphiti, fresh InMemorySaver",
        "reply": sanitized_role(dumped["reply"]),
        "reply_fallback": sanitized_role(dumped["reply_fallback"])
        if isinstance(dumped["reply_fallback"], dict)
        else None,
        "response_timeout_seconds": settings.conversation.response_timeout_seconds,
        "recall": settings.memory.recall.model_dump(mode="json"),
        "speech_language": settings.speech.language,
        "max_spoken_sentences": settings.conversation.max_spoken_sentences,
        "sentence_streaming": settings.conversation.sentence_streaming,
        "deadline_enforced": True,
        "model_token_budget_enforced": False,
        "token_usage_collected": True,
        "unproven": [
            "activation",
            "participation",
            "generation arbitration",
            "audio",
            "live stop/cancellation",
            "persistent checkpoint lifecycle",
        ],
    }


def prepare_artifact_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("manifest.json", "turns.json", "prompts.json"):
        if (directory / name).exists():
            raise FileExistsError(f"raw eval artifact already exists: {directory / name}")


class ModelCallCounter(AsyncCallbackHandler):
    def __init__(self) -> None:
        self.calls = 0
        self.trajectory: list[dict[str, Any]] = []
        self.tool_results: list[dict[str, Any]] = []
        self.prefetch: list[dict[str, Any]] = []
        self.usage: list[dict[str, Any] | None] = []
        self.running: dict[Any, tuple[dict[str, Any], float]] = {}

    @override
    async def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> None:
        self.calls += 1
        recorded: set[Any] = set()
        for batch in messages:
            for message in batch:
                details = (message.additional_kwargs or {}).get("memory_prefetch")
                if details and message.id not in recorded:
                    recorded.add(message.id)
                    self.prefetch.append({**details, "id": message.id, "output": message.text})
        latest = {
            message.tool_call_id: message
            for batch in messages
            for message in batch
            if isinstance(message, ToolMessage)
        }
        for tool_call_id, message in latest.items():
            for entry in reversed(self.trajectory):
                if entry["tool_call_id"] == tool_call_id and entry.get("exception"):
                    if not entry["output"]:
                        entry["output"] = message.text
                        entry["output_source"] = "native middleware ToolMessage sent to model"
                    break

    @override
    async def on_tool_start(
        self, serialized: Any, input_str: str, *, run_id: Any, **kwargs: Any
    ) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "tool"
        inputs = kwargs.get("inputs")
        if inputs is None:
            try:
                inputs = json.loads(input_str)
            except (ValueError, TypeError):
                inputs = input_str
        entry = {
            "name": str(name),
            "input": inputs,
            "output": "",
            "duration_ms": 0.0,
            "exception": None,
            "tool_call_id": kwargs.get("tool_call_id"),
        }
        if isinstance(inputs, dict) and "query" in inputs:
            entry["query"] = inputs["query"]
        self.trajectory.append(entry)
        self.running[run_id] = (entry, time.monotonic())

    @override
    async def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        entry, started = self.running.pop(run_id)
        entry["output"] = output.text if isinstance(output, ToolMessage) else str(output)
        entry["duration_ms"] = (time.monotonic() - started) * 1000

    @override
    async def on_tool_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        entry, started = self.running.pop(run_id)
        entry["exception"] = f"{type(error).__name__}: {error}"
        entry["duration_ms"] = (time.monotonic() - started) * 1000

    @override
    async def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        message = None
        for batch in getattr(response, "generations", None) or []:
            for generation in batch:
                candidate = getattr(generation, "message", None)
                if candidate is not None:
                    message = candidate
        meta = getattr(message, "usage_metadata", None) if message is not None else None
        self.usage.append(dict(meta) if meta else None)


def record_tool_results(trajectory: list[dict[str, Any]]) -> ToolResultCallback:
    def record(name: str, output: str, duration_ms: float) -> None:
        trajectory.append({"name": name, "output": output, "duration_ms": duration_ms})

    return record


@dataclass(frozen=True, kw_only=True, slots=True)
class ResponderContext:
    settings: Settings
    profile: PersonaProfile
    meeting_id: str
    memory: Graphiti | None
    brief: MeetingBrief | None
    fallback: BaseChatModel | None
    counter: ModelCallCounter
    trajectory: list[dict[str, Any]]


@asynccontextmanager
async def _open_native_conversation(
    model: BaseChatModel,
    context: ResponderContext,
    settings: Settings,
) -> AsyncIterator[LangGraphResponder]:
    profile = context.profile

    @asynccontextmanager
    async def checkpointer(url: str, **_kwargs: Any) -> AsyncIterator[InMemorySaver]:
        del url
        yield InMemorySaver()

    config = SessionConfig(
        owner_id=profile.owner_id,
        twin_id=profile.twin_id,
        meeting_id=SessionId(context.meeting_id),
        link="https://meet.google.com/eval-only",
        twin_name=profile.display_name,
    )
    context.counter.trajectory = context.trajectory
    with (
        patch.object(assembly, "create_chat_model", return_value=model),
        patch.object(assembly, "create_fallback_chat_model", return_value=context.fallback),
        patch.object(assembly, "open_checkpointer", checkpointer),
        patch.object(assembly, "require_url", lambda url: url or "eval-in-memory"),
    ):
        async with assembly.open_conversation(
            settings,
            config,
            agent=assembly.AgentWiring(
                profile=profile, brief=context.brief, graphiti=context.memory
            ),
            behavior=assembly.TurnBehavior(
                on_transition=lambda *_: None,
                wake_phrases=(profile.display_name,),
                on_tool_result=record_tool_results(context.counter.tool_results),
            ),
        ) as conversation:
            responder = conversation.responder
            if not isinstance(responder, LangGraphResponder):
                raise TypeError("expected the native non-streaming responder")
            yield replace(
                responder,
                agent=responder.agent.with_config(callbacks=[context.counter]),
            )


@asynccontextmanager
async def native_responder(
    model: BaseChatModel,
    context: ResponderContext,
) -> AsyncIterator[LangGraphResponder]:
    settings = context.settings.model_copy(
        update={
            "conversation": context.settings.conversation.model_copy(
                update={"sentence_streaming": False}
            ),
            "participation_decider": context.settings.participation_decider.model_copy(
                update={"enabled": False}
            ),
        }
    )
    if settings.conversation.max_spoken_sentences > 2:
        raise ValueError("these evals require a spoken cap of at most two sentences")
    async with _open_native_conversation(model, context, settings) as responder:
        yield responder


async def respond_with_deadline(
    responder: LangGraphResponder, event: TranscriptEvent, settings: Settings
) -> str:
    async with asyncio.timeout(settings.conversation.response_timeout_seconds):
        return await responder.respond([event], lambda: None)


@asynccontextmanager
async def native_streaming_responder(
    model: BaseChatModel,
    context: ResponderContext,
) -> AsyncIterator[LangGraphResponder]:
    if not context.settings.conversation.sentence_streaming:
        raise ValueError("the streaming eval mode requires sentence streaming")
    if context.settings.conversation.max_spoken_sentences > 2:
        raise ValueError("these evals require a spoken cap of at most two sentences")
    async with _open_native_conversation(model, context, context.settings) as responder:
        yield responder


async def stream_with_deadline(
    responder: LangGraphResponder,
    event: TranscriptEvent,
    settings: Settings,
    *,
    request_id: str,
) -> tuple[list[str], float]:
    started = time.monotonic()
    sentences: list[str] = []
    first_latency = 0.0
    async with asyncio.timeout(settings.conversation.response_timeout_seconds):
        async for sentence in stream_sentences(
            responder.agent,
            request_messages([event]),
            responder.thread_config(responder.thread_id),
            max_sentences=responder.max_sentences,
            trace=StreamTrace(
                language=responder.language,
                journal=responder.journal,
                request_id=request_id,
                transcript_id=event.id,
                on_tool_result=responder.on_tool_result,
            ),
        ):
            if not sentences:
                first_latency = time.monotonic() - started
            sentences.append(sentence)
    return sentences, first_latency
