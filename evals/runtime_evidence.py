import asyncio
import hashlib
import json
import subprocess
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

import fakeyou.orchestration.assembly as assembly
from evals.test_replay_eval import _sanitized_role
from fakeyou.config.settings import Settings
from fakeyou.conversation.conversation import LangGraphResponder, ToolResultCallback
from fakeyou.conversation.session import SessionConfig
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.memory.persona import PersonaProfile
from fakeyou.persistence.brief import MeetingBrief

ROOT = Path(__file__).resolve().parent.parent
CANDIDATE_FILES = (
    "src/fakeyou/__init__.py",
    "src/fakeyou/agent.py",
    "src/fakeyou/assembly.py",
    "src/fakeyou/audio.py",
    "src/fakeyou/brief.py",
    "src/fakeyou/browser.py",
    "src/fakeyou/checkpoint.py",
    "src/fakeyou/cli/join.py",
    "src/fakeyou/cli/__init__.py",
    "src/fakeyou/cli/chat.py",
    "src/fakeyou/cli/diagnostics.py",
    "src/fakeyou/cli/errors.py",
    "src/fakeyou/cli/main.py",
    "src/fakeyou/cli/memory.py",
    "src/fakeyou/conversation.py",
    "src/fakeyou/cleanup.py",
    "src/fakeyou/deaf.py",
    "src/fakeyou/faults.py",
    "src/fakeyou/greetings.py",
    "src/fakeyou/greetings_vocabulary.json",
    "src/fakeyou/ingestion_status.py",
    "src/fakeyou/ingestion.py",
    "src/fakeyou/invariants.py",
    "src/fakeyou/journal.py",
    "src/fakeyou/lifecycle.py",
    "src/fakeyou/memory.py",
    "src/fakeyou/meet.py",
    "src/fakeyou/owner_notes.py",
    "src/fakeyou/participants.py",
    "src/fakeyou/participant_observer.py",
    "src/fakeyou/participation.py",
    "src/fakeyou/persona.py",
    "src/fakeyou/playback.py",
    "src/fakeyou/preflight.py",
    "src/fakeyou/provider_faults.py",
    "src/fakeyou/providers.py",
    "src/fakeyou/provenance.py",
    "src/fakeyou/record.py",
    "src/fakeyou/report.py",
    "src/fakeyou/runtime.py",
    "src/fakeyou/session.py",
    "src/fakeyou/settings.py",
    "src/fakeyou/taxonomy.py",
    "src/fakeyou/voice.py",
    "src/fakeyou/watchdog.py",
    "evals/runtime_evidence.py",
    "evals/test_brief_eval.py",
    "evals/test_trust_eval.py",
    "evals/test_replay_eval.py",
    "evals/fixtures/brief-scenarios.json",
    "evals/fixtures/trust-scenarios.json",
    "examples/persona.example.json",
    "tests/fakes.py",
    "pyproject.toml",
    "uv.lock",
)


def _file_hashes(root: Path, paths: list[str]) -> dict[str, str | None]:
    return {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        if (root / name).is_file()
        else None
        for name in paths
    }


def _digest(hashes: dict[str, str | None]) -> str:
    return hashlib.sha256(
        json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def capture_candidate(fixture_path: Path, *, root: Path = ROOT) -> dict[str, Any]:
    fixture_name = fixture_path.resolve().relative_to(root.resolve()).as_posix()
    paths = sorted(set(CANDIDATE_FILES) | {fixture_name})
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
        "schema": "eval_candidate/v1",
        "started_at": datetime.now(UTC).isoformat(),
        "source_commit": commit,
        "dirty_at_start": dirty,
        "start_sha256": _digest(hashes),
        "start_files": hashes,
        "fixture_sha256": hashes[fixture_name],
        "scope": "fixed A/runtime/eval allowlist; excludes debrief B, private data and .env",
    }


def finish_candidate(candidate: dict[str, Any], *, root: Path = ROOT) -> dict[str, Any]:
    hashes = _file_hashes(root, sorted(candidate["start_files"]))
    changed = [name for name, digest in hashes.items() if candidate["start_files"][name] != digest]
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
        "proof_scope": "native non-streaming agent/responder; not full ConversationLoop/audio",
        "assembly": "fakeyou.orchestration.assembly.open_conversation",
        "boundaries": "supplied model/fallback, synthetic Graphiti, fresh InMemorySaver",
        "reply": _sanitized_role(dumped["reply"]),
        "reply_fallback": _sanitized_role(dumped["reply_fallback"])
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

    async def on_tool_end(self, output: Any, *, run_id: Any, **kwargs: Any) -> None:
        entry, started = self.running.pop(run_id)
        entry["output"] = output.text if isinstance(output, ToolMessage) else str(output)
        entry["duration_ms"] = (time.monotonic() - started) * 1000

    async def on_tool_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        entry, started = self.running.pop(run_id)
        entry["exception"] = f"{type(error).__name__}: {error}"
        entry["duration_ms"] = (time.monotonic() - started) * 1000

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


@asynccontextmanager
async def native_responder(
    model: BaseChatModel,
    *,
    settings: Settings,
    profile: PersonaProfile,
    meeting_id: str,
    memory: Any,
    brief: MeetingBrief | None,
    fallback: BaseChatModel | None,
    counter: ModelCallCounter,
    trajectory: list[dict[str, Any]],
) -> AsyncIterator[LangGraphResponder]:
    if settings.conversation.sentence_streaming:
        raise ValueError("these evals prove only the non-streaming native responder")
    if settings.conversation.max_spoken_sentences > 2:
        raise ValueError("these evals require a spoken cap of at most two sentences")

    @asynccontextmanager
    async def checkpointer(url: str) -> AsyncIterator[InMemorySaver]:
        yield InMemorySaver()

    config = SessionConfig(
        owner_id=profile.owner_id,
        twin_id=profile.twin_id,
        meeting_id=meeting_id,
        link="https://meet.google.com/eval-only",
        twin_name=profile.display_name,
    )
    counter.trajectory = trajectory
    with (
        patch.object(assembly, "create_chat_model", return_value=model),
        patch.object(assembly, "create_fallback_chat_model", return_value=fallback),
        patch.object(assembly, "open_checkpointer", checkpointer),
    ):
        async with assembly.open_conversation(
            settings,
            config,
            graphiti=memory,
            profile=profile,
            brief=brief,
            on_transition=lambda *_: None,
            wake_phrases=(profile.display_name,),
            on_tool_result=record_tool_results(counter.tool_results),
        ) as conversation:
            responder = conversation._responder
            if not isinstance(responder, LangGraphResponder):
                raise TypeError("expected the native non-streaming responder")
            yield replace(responder, agent=responder.agent.with_config(callbacks=[counter]))


async def respond_with_deadline(
    responder: LangGraphResponder, event: TranscriptEvent, settings: Settings
) -> str:
    async with asyncio.timeout(settings.conversation.response_timeout_seconds):
        return await responder.respond([event], lambda: None)
