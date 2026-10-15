import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from contextlib import ExitStack, nullcontext
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, override
from unittest.mock import patch

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.runnables.config import var_child_runnable_config

import fakeyou.cli.debrief as cli_debrief
from evals._debrief_support import (
    DebriefReview,
    capture_debrief_candidate,
    case_brief,
    contract_reviews,
    debrief_gate,
    load_debrief_fixture,
    record_bytes,
    source_index,
)
from evals.runtime_evidence import finish_candidate
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import OpenAIReply, PathSettings, Settings
from fakeyou.debrief.debrief_sources import chunk_sources
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile, persona_digest
from fakeyou.observability.faults import capture_error
from fakeyou.persistence.brief import (
    MeetingContext,
    brief_digest,
    context_path,
    write_meeting_context,
)
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    record_lock,
    status_path,
    write_status,
)
from fakeyou.persistence.record import meeting_record_path
from tests._conversation_fakes import ToolCallingFakeModel
from tests._error_support import message_pattern
from tests._fakes_common import env_file_opt


async def test_offline_native_cli_runner_saves_immutable_reports_and_pending_review(
    offline_settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path / "raw"
    outcome = await replay_debriefs(
        offline_settings, directory, model_factory=contract_debrief_model
    )
    assert len(outcome["results"]) == 8
    assert sum(item["status"] == "completed" for item in outcome["results"]) == 7
    assert outcome["review_status"] == "pending"
    assert outcome["semantic_pass"] is False
    assert outcome["candidate"]["unchanged"] is True
    assert all(
        item["model_calls"] > 0 for item in outcome["results"] if item["status"] == "completed"
    )
    assert outcome["results"][-1]["model_calls"] == 0
    assert (directory / "manifest.json").is_file()
    with pytest.raises(FileExistsError):
        await replay_debriefs(offline_settings, directory, model_factory=contract_debrief_model)


def contract_debrief_model(case: dict[str, Any]) -> BaseChatModel:
    sources = source_index(case)
    chunks = chunk_sources(tuple(sources.values()))
    replies: list[BaseMessage] = []
    for number, chunk in enumerate(chunks):
        ids = {entry.id for entry in chunk}
        items = [
            dict(
                kind=item["kind"],
                status=item["statuses"][0],
                text=item["meaning"],
                evidence=item["evidence"],
                assignee_claim=item.get("assignee_claim"),
                due_text=item.get("due_text"),
            )
            for item in case["required_items"]
            if all(ref["entry_id"] in ids for ref in item["evidence"])
        ]
        findings = [
            dict(objective_id=item["id"], status=item["statuses"][0], evidence=[])
            for item in case.get("required_findings", [])
        ]
        replies.append(
            AIMessage(
                content="",
                tool_calls=[
                    dict(
                        name="DebriefDraft",
                        id=f"draft-{number}",
                        args=dict(items=items, findings=findings),
                    )
                ],
            )
        )
    return ToolCallingFakeModel(responses=replies)


@pytest.fixture
def offline_settings(tmp_path: Path) -> Settings:
    return Settings(
        **env_file_opt(None),
        reply=OpenAIReply(model="offline-contract"),
        paths=PathSettings(record_dir=tmp_path / "meetings", journal_dir=tmp_path / "journals"),
    )


class DebriefCallCounter(AsyncCallbackHandler):
    def __init__(self) -> None:
        self.calls = 0
        self.errors = 0
        self.raw_calls: list[dict[str, Any]] = []
        self.running: dict[Any, tuple[dict[str, Any], float]] = {}

    @override
    async def on_chat_model_start(
        self, serialized: Any, messages: Any, *, run_id: Any, **kwargs: Any
    ) -> None:
        self.calls += 1
        call = dict(
            messages=[[message.model_dump(mode="json") for message in batch] for batch in messages],
            provider_metadata=serialized.get("name") if serialized else None,
        )
        self.raw_calls.append(call)
        self.running[run_id] = (call, time.monotonic())

    @override
    async def on_llm_end(self, response: Any, *, run_id: Any, **kwargs: Any) -> None:
        call, started = self.running.pop(run_id)
        call.update(
            response=response.model_dump(mode="json", serialize_as_any=True),
            latency_ms=(time.monotonic() - started) * 1000,
            error=None,
        )

    @override
    async def on_llm_error(self, error: BaseException, *, run_id: Any, **kwargs: Any) -> None:
        self.errors += 1
        call, started = self.running.pop(run_id)
        call.update(latency_ms=(time.monotonic() - started) * 1000, error=type(error).__name__)


async def test_native_callback_preserves_structured_tool_arguments() -> None:
    counter = DebriefCallCounter()
    await counter.on_chat_model_start({}, [[AIMessage(content="Synthetic source")]], run_id="r1")
    message = AIMessage(
        content="",
        tool_calls=[{"name": "DebriefDraft", "id": "draft", "args": {"items": [], "findings": []}}],
    )
    await counter.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=message)]]), run_id="r1"
    )
    saved = counter.raw_calls[0]["response"]["generations"][0][0]["message"]
    assert saved["tool_calls"] == message.tool_calls


def _write_json_fresh(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _stage_case(
    settings: Settings, directory: Path, fixture: dict[str, Any], case: dict[str, Any], run: int
) -> tuple[Settings, Path, Path, str]:
    staged = directory / f"{case['id']}-{run}"
    staged.mkdir()
    paths = settings.paths.model_copy(
        update={"record_dir": staged / "meetings", "journal_dir": staged / "journals"}
    )
    scoped = settings.model_copy(update={"paths": paths})
    profile = PersonaProfile.model_validate(fixture["persona"])
    persona_path = staged / "persona.json"
    persona_path.write_text(profile.model_dump_json(), encoding="utf-8")
    meeting_id = f"debrief-eval-{case['id']}-{run}"
    record = meeting_record_path(paths.record_dir, SessionId(meeting_id))
    record.parent.mkdir(parents=True)
    record.write_bytes(record_bytes(case))
    write_status(
        status_path(record),
        IngestionStatus(
            meeting_id=meeting_id, group_id=profile.memory_group, status="not_requested"
        ),
    )
    brief = case_brief(fixture, case, meeting_id)
    if brief is not None:
        write_meeting_context(
            context_path(record),
            MeetingContext(
                schema_version="meeting_context/v1",
                brief=brief,
                brief_sha256=brief_digest(brief),
                persona_sha256=persona_digest(profile),
            ),
        )
    return scoped, persona_path, record, meeting_id


def _provider_metadata(settings: Settings) -> dict[str, Any]:
    def role(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return {
            name: getattr(value, name)
            for name in ("provider", "model", "timeout_seconds", "max_retries")
        }

    return dict(
        primary=role(settings.debrief_role),
        fallback=role(settings.reply_fallback),
        response_timeout_seconds=settings.conversation.response_timeout_seconds,
    )


async def replay_debriefs(
    settings: Settings,
    directory: Path,
    *,
    model_factory: Callable[[dict[str, Any]], BaseChatModel] | None = None,
    runs: int = 1,
) -> dict[str, Any]:
    if runs < 1:
        raise ValueError("debrief eval runs must be positive")
    await asyncio.to_thread(_prepare_directory, directory)
    fixture = load_debrief_fixture()
    candidate = capture_debrief_candidate()
    _write_json_fresh(directory / "fixture.json", fixture)
    _write_json_fresh(directory / "candidate-start.json", candidate)
    results: list[dict[str, Any]] = []
    for case in fixture["cases"]:
        for run in range(runs):
            scoped, persona_path, record, meeting_id = _stage_case(
                settings, directory, fixture, case, run
            )
            counter = DebriefCallCounter()
            token = var_child_runnable_config.set({"callbacks": [counter]})
            started = time.monotonic()
            error = None
            with ExitStack() as stack:
                if model_factory is not None:
                    if case.get("expect") == "preprovider_failure":

                        def refused_provider(*args: Any, **_kwargs: Any) -> BaseChatModel:
                            del args
                            raise AssertionError("invalid source reached provider construction")

                        stack.enter_context(
                            patch.object(cli_debrief, "create_debrief_chat_model", refused_provider)
                        )
                    else:
                        model = model_factory(case)
                        stack.enter_context(
                            patch.object(
                                cli_debrief, "create_debrief_chat_model", return_value=model
                            )
                        )
                    stack.enter_context(
                        patch.object(cli_debrief, "create_fallback_chat_model", return_value=None)
                    )
                try:
                    _, failure = await capture_error(
                        partial(
                            cli_debrief.debrief_meeting,
                            meeting_id=SessionId(meeting_id),
                            identity=OperatorIdentity(
                                persona_path,
                                fixture["persona"]["owner_id"],
                                fixture["persona"]["twin_id"],
                            ),
                            force=False,
                            settings=scoped,
                        )
                    )
                    if failure is not None:
                        error = type(failure).__name__
                finally:
                    var_child_runnable_config.reset(token)
            report_path = record.parent / "debrief.json"
            report_json = report_path.read_text() if report_path.exists() else None
            expected_failure = (
                case.get("expect") == "preprovider_failure"
                and error == "DebriefValidationError"
                and counter.calls == 0
                and report_json is None
            )
            result = dict(
                case_id=case["id"],
                run=run,
                meeting_id=meeting_id,
                status="expected_failure"
                if expected_failure
                else "failed"
                if error
                else "completed",
                error=error,
                latency_ms=(time.monotonic() - started) * 1000,
                model_calls=counter.calls,
                provider_errors=counter.errors,
                raw_calls=counter.raw_calls,
                record_sha256=hashlib.sha256(record.read_bytes()).hexdigest(),
                raw_record=record.read_text(),
                report_json=report_json,
                report_sha256=hashlib.sha256(report_path.read_bytes()).hexdigest()
                if report_json
                else None,
                provider=_provider_metadata(scoped),
                context_json=context_path(record).read_text()
                if context_path(record).exists()
                else None,
            )
            if report_json is not None:
                report = json.loads(report_json)
                _write_json_fresh(
                    record.parent / "draft.json",
                    {"items": report["items"], "findings": report["findings"]},
                )
            _write_json_fresh(directory / f"{case['id']}-{run}" / "result.json", result)
            results.append(result)
    candidate = finish_candidate(candidate)
    _write_json_fresh(directory / "results.json", results)
    hashes = await asyncio.to_thread(_artifact_hashes, directory)
    summary = dict(
        schema="debrief_eval_manifest/v1",
        mode="live" if model_factory is None else "offline_contract",
        candidate=candidate,
        runs=runs,
        results=results,
        raw_artifact_hashes=hashes,
        review_status="pending",
        semantic_pass=False,
        extraction_completed=sum(item["status"] == "completed" for item in results),
        expected_source_failures=sum(item["status"] == "expected_failure" for item in results),
        unexpected_errors=sum(item["status"] == "failed" for item in results),
        provider_errors=sum(item["provider_errors"] for item in results),
        model_calls=sum(item["model_calls"] for item in results),
        latencies_ms=[item["latency_ms"] for item in results],
        provider=_provider_metadata(settings),
        review_contract=DebriefReview.model_json_schema(),
        coverage_threshold=0.9,
    )
    _write_json_fresh(directory / "manifest.json", summary)
    await asyncio.to_thread(_freeze_artifacts, directory)
    return summary


def review_debrief_artifacts(
    directory: Path, reviews_path: Path, output_path: Path
) -> dict[str, Any]:
    manifest = json.loads((directory / "manifest.json").read_text())
    fixture = json.loads((directory / "fixture.json").read_text())
    reviews = json.loads(reviews_path.read_text())
    results = json.loads((directory / "results.json").read_text())
    results_match = results == manifest["results"]
    intact = all(
        (directory / name).is_file()
        and hashlib.sha256((directory / name).read_bytes()).hexdigest() == digest
        for name, digest in manifest["raw_artifact_hashes"].items()
    )
    accepted = (
        intact
        and results_match
        and manifest["candidate"]["unchanged"] is True
        and debrief_gate(reviews, results, fixture, runs=manifest["runs"], mode=manifest["mode"])
    )
    result = dict(
        schema="debrief_eval_review/v1",
        reviewed_at=datetime.now(UTC).isoformat(),
        raw_artifacts_intact=intact,
        results_match=results_match,
        candidate_unchanged=manifest["candidate"]["unchanged"],
        review_status="accepted" if accepted else "rejected",
        semantic_pass=accepted,
        review_sha256=hashlib.sha256(reviews_path.read_bytes()).hexdigest(),
        manifest_sha256=hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest(),
        coverage_threshold=0.9,
    )
    _write_json_fresh(output_path, result)
    return result


@pytest.mark.live
async def test_live_debrief_native_cli_corpus(tmp_path: Path) -> None:
    configured = os.environ.get("DEBRIEF_ARTIFACT_DIR")
    directory = Path(configured) if configured else tmp_path / "debrief-live"
    outcome = await replay_debriefs(Settings(), directory)
    assert outcome["candidate"]["unchanged"], outcome["candidate"]["changed_files"]
    assert outcome["extraction_completed"] == 7
    assert outcome["expected_source_failures"] == 1
    assert outcome["unexpected_errors"] == 0
    reviews = os.environ.get("DEBRIEF_REVIEWS_PATH")
    if reviews:
        result = review_debrief_artifacts(
            directory, Path(reviews), directory.parent / f"{directory.name}-review.json"
        )
        assert result["semantic_pass"], result
    else:
        assert outcome["review_status"] == "pending"


INVALID_SOURCE_CODES = {
    "missing": ErrorCode.DEBRIEF_RECORD_MISSING,
    "corrupt": ErrorCode.DEBRIEF_RECORD_INVALID_LINE,
    "oversized_entry": ErrorCode.DEBRIEF_SOURCE_EXCEEDS_CHUNK,
    "too_many_chunks": ErrorCode.DEBRIEF_TOO_MANY_CHUNKS,
    "lock": ErrorCode.RECORD_LOCKED,
    "corrupt_context": ErrorCode.DEBRIEF_CONTEXT_INVALID,
}


@pytest.mark.parametrize(
    "invalid",
    ["missing", "corrupt", "oversized_entry", "too_many_chunks", "lock", "corrupt_context"],
)
async def test_cli_invalid_sources_fail_before_provider_construction(
    offline_settings: Settings, tmp_path: Path, invalid: str
) -> None:
    fixture = load_debrief_fixture()
    case = fixture["cases"][0]
    directory = tmp_path / invalid
    directory.mkdir()
    scoped, persona, record, meeting_id = _stage_case(offline_settings, directory, fixture, case, 0)
    if invalid == "missing":
        record.unlink()
    elif invalid == "corrupt":
        record.write_text('{"kind":')
    elif invalid == "oversized_entry":
        entry = {**case["entries"][0], "text": "x" * 16001}
        record.write_text(json.dumps(entry) + "\n")
    elif invalid == "too_many_chunks":
        record.write_text(
            "".join(
                json.dumps({**case["entries"][0], "id": f"limit-{index}"}) + "\n"
                for index in range(725)
            )
        )
    elif invalid == "corrupt_context":
        context_path(record).write_text("{}")

    def provider_forbidden(*args: Any, **_kwargs: Any) -> BaseChatModel:
        del args
        pytest.fail("invalid source reached provider construction")

    with (
        patch.object(cli_debrief, "create_debrief_chat_model", provider_forbidden),
        patch.object(cli_debrief, "create_fallback_chat_model", provider_forbidden),
        record_lock(record, purpose="meeting") if invalid == "lock" else nullcontext(),
        pytest.raises(FakeYouError, match=message_pattern(INVALID_SOURCE_CODES[invalid])) as caught,
    ):
        await cli_debrief.debrief_meeting(
            meeting_id=SessionId(meeting_id),
            identity=OperatorIdentity(
                persona, fixture["persona"]["owner_id"], fixture["persona"]["twin_id"]
            ),
            force=False,
            settings=scoped,
        )
    assert caught.value.code is INVALID_SOURCE_CODES[invalid]
    assert not (record.parent / "debrief.json").exists()


async def test_saved_raw_evidence_tampering_fails_independent_review(
    offline_settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path / "raw-review"
    outcome = await replay_debriefs(
        offline_settings, directory, model_factory=contract_debrief_model
    )
    fixture = load_debrief_fixture()
    reviews_path = tmp_path / "reviews.json"
    _write_json_fresh(reviews_path, contract_reviews(fixture, outcome["results"]))
    accepted = review_debrief_artifacts(directory, reviews_path, tmp_path / "accepted.json")
    assert accepted["semantic_pass"] is True
    record = next(directory.rglob("record.jsonl"))
    record.chmod(0o644)
    record.write_text(record.read_text() + "\n")
    rejected = review_debrief_artifacts(directory, reviews_path, tmp_path / "rejected.json")
    assert rejected["semantic_pass"] is False
    assert rejected["raw_artifacts_intact"] is False


async def test_manifest_result_override_cannot_replace_hashed_outputs(
    offline_settings: Settings, tmp_path: Path
) -> None:
    directory = tmp_path / "raw-manifest-review"
    outcome = await replay_debriefs(
        offline_settings, directory, model_factory=contract_debrief_model
    )
    reviews_path = tmp_path / "reviews.json"
    _write_json_fresh(reviews_path, contract_reviews(load_debrief_fixture(), outcome["results"]))
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["results"][0]["latency_ms"] += 1
    manifest_path.chmod(0o644)
    manifest_path.write_text(json.dumps(manifest))
    result = review_debrief_artifacts(directory, reviews_path, tmp_path / "rejected.json")
    assert result["semantic_pass"] is False
    assert result["results_match"] is False


def _prepare_directory(directory: Path) -> None:
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(exist_ok=False)


def _artifact_hashes(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _freeze_artifacts(directory: Path) -> None:
    for path in directory.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
