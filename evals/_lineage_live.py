import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from google.genai import errors as genai_errors
from langchain.chat_models import init_chat_model
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from evals._decision_lineage import FIXTURE, LABELS, load_dataset
from evals._lineage_classifier import (
    PILOT_FIXTURE,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    CallFailedError,
    CassetteEntry,
    CassetteFailure,
    ClassifierRequest,
    PilotPlan,
    Provenance,
    RunStoppedError,
    StopReason,
    load_plan,
    pilot_scope,
    replay_report,
    spend,
)
from fakeyou.catalog.config import ConfigKey
from fakeyou.config.settings import GoogleReply, Settings, require_secret
from fakeyou.observability.provider_faults import ProviderFault, classify_exception, classify_http

GEMINI_SCHEMA_KEYWORDS = frozenset(
    {"type", "properties", "required", "items", "enum", "maxItems", "additionalProperties"}
)
_STRICT = ConfigDict(extra="forbid", frozen=True)


class TokenUsage(BaseModel):
    model_config = _STRICT

    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    thinking_tokens: Annotated[int, Field(ge=0)]


def response_schema(request: ClassifierRequest) -> dict[str, object]:
    refs = [view.ref for view in request.candidates]
    entries = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "comparisons": {
                "type": "array",
                "maxItems": len(refs),
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate": {"type": "string", "enum": refs},
                        "label": {"type": "string", "enum": list(LABELS)},
                        "earlier_evidence": entries,
                        "later_evidence": entries,
                    },
                    "required": ["candidate", "label", "earlier_evidence", "later_evidence"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["comparisons"],
        "additionalProperties": False,
    }


def messages(request: ClassifierRequest) -> list[BaseMessage]:
    return [SystemMessage(SYSTEM_PROMPT), HumanMessage(request.model_dump_json())]


def input_token_bound(request: ClassifierRequest, overhead: int) -> int:
    payload = SYSTEM_PROMPT + request.model_dump_json() + json.dumps(response_schema(request))
    return len(payload.encode("utf-8")) + overhead


def call_options(plan: PilotPlan, request: ClassifierRequest) -> dict[str, object]:
    return {
        "response_mime_type": "application/json",
        "response_json_schema": response_schema(request),
        "service_tier": plan.live.service_tier,
    }


class ChatModel(Protocol):
    def invoke(self, input: list[BaseMessage], **kwargs: Any) -> BaseMessage: ...


def create_model(plan: PilotPlan, api_key: str) -> ChatModel:
    model: ChatModel = init_chat_model(
        plan.model,
        model_provider="google_genai",
        api_key=api_key,
        timeout=plan.live.timeout_seconds,
        max_retries=1,
        reasoning_effort=plan.reasoning_effort,
        max_output_tokens=plan.caps.max_output_tokens_per_call,
    )
    return model


def classify_fault(error: Exception) -> ProviderFault:
    if isinstance(error, genai_errors.APIError):
        return classify_http("llm", error.code, error.message)
    return classify_exception("llm", error)


def _usage(message: BaseMessage) -> TokenUsage | None:
    usage = getattr(message, "usage_metadata", None)
    if not usage:
        return None
    details = usage.get("output_token_details") or {}
    return TokenUsage(
        input_tokens=usage["input_tokens"],
        output_tokens=usage["output_tokens"],
        thinking_tokens=details.get("reasoning", 0),
    )


@dataclass
class Ledger:
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    reserved_usd: float = 0.0
    consecutive_errors: int = 0
    failed_attempts: int = 0

    def summary(self, plan: PilotPlan) -> dict[str, object]:
        return {
            "attempts": self.attempts,
            "failed_attempts": self.failed_attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "reserved_for_unknown_usage_usd": round(self.reserved_usd, 6),
            "spend_usd": round(self.spend(plan), 6),
        }

    def spend(self, plan: PilotPlan) -> float:
        return spend(plan.pricing, self.input_tokens, self.output_tokens) + self.reserved_usd


class GeminiPilotProvider:
    def __init__(
        self,
        model: ChatModel,
        plan: PilotPlan,
        provenance: Provenance,
        sink: Callable[[str], None],
        secret: str,
    ) -> None:
        self.model = model
        self.plan = plan
        self.provenance = provenance
        self.sink = sink
        self.secret = secret
        self.ledger = Ledger()
        self.calls = 0

    def _worst(self, bound: int) -> float:
        return spend(self.plan.pricing, bound, self.plan.caps.max_output_tokens_per_call)

    def _admit(self, bound: int) -> None:
        caps = self.plan.caps
        reason: StopReason | None = None
        if bound > self.plan.live.max_input_tokens_per_call:
            reason = "input_bound_exceeded"
        elif self.ledger.attempts >= caps.max_calls:
            reason = "call_cap"
        elif self.ledger.input_tokens + bound > caps.max_input_tokens:
            reason = "input_token_cap"
        elif self.ledger.output_tokens + caps.max_output_tokens_per_call > caps.max_output_tokens:
            reason = "output_token_cap"
        elif self.ledger.spend(self.plan) + self._worst(bound) > caps.max_spend_usd:
            reason = "spend_cap"
        if reason is not None:
            raise RunStoppedError(reason)

    def _write(self, line: CassetteEntry | CassetteFailure) -> None:
        text = line.model_dump_json()
        if self.secret and self.secret in text:
            raise RunStoppedError("secret_in_record")
        self.sink(text + "\n")

    def _failure(
        self,
        request: ClassifierRequest,
        attempt: int,
        outcome: Literal["error", "usage_unknown"],
        fault: ProviderFault | None,
        response: str | None,
    ) -> None:
        self._write(
            CassetteFailure(
                outcome=outcome,
                request_digest=request.digest(self.plan.model),
                model=self.plan.model,
                prompt_version=PROMPT_VERSION,
                call=self.calls,
                attempt=attempt,
                error_kind=fault.kind if fault else None,
                error_status=fault.status if fault else None,
                error_detail=fault.detail if fault else None,
                response=response,
                provenance=self.provenance,
            )
        )

    def respond(self, request: ClassifierRequest, model: str) -> CassetteEntry:
        if model != self.plan.model:
            raise RunStoppedError("model_mismatch")
        self.calls += 1
        bound = input_token_bound(request, self.plan.live.message_overhead_tokens)
        for attempt in range(1, self.plan.live.max_attempts_per_call + 1):
            self._admit(bound)
            self.ledger.attempts += 1
            try:
                message = self.model.invoke(messages(request), **call_options(self.plan, request))
            except Exception as error:
                fault = classify_fault(error)
                self.ledger.failed_attempts += 1
                self.ledger.consecutive_errors += 1
                self.ledger.reserved_usd += self._worst(bound)
                self._failure(request, attempt, "error", fault, None)
                if not fault.retryable:
                    raise RunStoppedError("provider_rejected") from error
                if (
                    self.ledger.consecutive_errors
                    >= self.plan.stop_rules.consecutive_provider_errors
                ):
                    raise RunStoppedError("provider_errors") from error
                continue
            self.ledger.consecutive_errors = 0
            return self._accept(request, attempt, message, bound)
        raise CallFailedError(request.digest(self.plan.model))

    def _accept(
        self, request: ClassifierRequest, attempt: int, message: BaseMessage, bound: int
    ) -> CassetteEntry:
        usage = _usage(message)
        if usage is None:
            self.ledger.reserved_usd += self._worst(bound)
            self._failure(request, attempt, "usage_unknown", None, message.text)
            raise RunStoppedError("usage_unknown")
        self.ledger.input_tokens += usage.input_tokens
        self.ledger.output_tokens += usage.output_tokens
        self.ledger.thinking_tokens += usage.thinking_tokens
        entry = CassetteEntry(
            request_digest=request.digest(self.plan.model),
            model=self.plan.model,
            prompt_version=PROMPT_VERSION,
            response=message.text,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            thinking_tokens=usage.thinking_tokens,
            call=self.calls,
            attempt=attempt,
            provenance=self.provenance,
        )
        self._write(entry)
        if (
            usage.input_tokens > bound
            or usage.output_tokens > self.plan.caps.max_output_tokens_per_call
        ):
            raise RunStoppedError("usage_over_bound")
        if self.ledger.spend(self.plan) > self.plan.caps.max_spend_usd:
            raise RunStoppedError("spend_cap")
        return entry


_Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
_Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ExecutionApproval(BaseModel):
    model_config = _STRICT

    approved_by: Annotated[str, StringConstraints(min_length=1)]
    approved_on: date
    head: _Sha
    plan_sha256: _Sha256
    model: str
    max_calls: int
    max_spend_usd: float


def plan_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def approval_problems(
    approval: ExecutionApproval, plan: PilotPlan, plan_sha256: str, head: str
) -> list[str]:
    checks = {
        "head": (approval.head, head),
        "plan_sha256": (approval.plan_sha256, plan_sha256),
        "model": (approval.model, plan.model),
        "max_calls": (approval.max_calls, plan.caps.max_calls),
        "max_spend_usd": (approval.max_spend_usd, plan.caps.max_spend_usd),
    }
    return [
        f"{name}: approved {approved}, running {running}"
        for name, (approved, running) in checks.items()
        if approved != running
    ]


def preflight(plan: PilotPlan, requests: Sequence[ClassifierRequest]) -> dict[str, object]:
    bounds = [input_token_bound(request, plan.live.message_overhead_tokens) for request in requests]
    attempts = plan.caps.max_calls
    worst_input = max(bounds, default=0) * attempts
    worst_output = attempts * plan.caps.max_output_tokens_per_call
    return {
        "calls": len(requests),
        "max_requests_including_retries": attempts,
        "input_token_bound": {
            "method": "UTF-8 bytes of system prompt, request and schema, plus a fixed overhead",
            "total_for_planned_calls": sum(bounds),
            "max_per_call": max(bounds, default=0),
        },
        "worst_case": {
            "input_tokens": worst_input,
            "output_tokens_including_thinking": worst_output,
            "spend_usd": round(spend(plan.pricing, worst_input, worst_output), 4),
        },
        "within_caps": max(bounds, default=0) <= plan.live.max_input_tokens_per_call
        and worst_input <= plan.caps.max_input_tokens
        and worst_output <= plan.caps.max_output_tokens
        and spend(plan.pricing, worst_input, worst_output) <= plan.caps.max_spend_usd,
    }


def git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def provenance_for(plan: PilotPlan, plan_sha256: str, head: str) -> Provenance:
    return Provenance(
        provider=plan.provider,
        model=plan.model,
        service_tier=plan.live.service_tier,
        thinking_level=plan.reasoning_effort,
        max_output_tokens=plan.caps.max_output_tokens_per_call,
        timeout_seconds=plan.live.timeout_seconds,
        langchain_google_genai=version("langchain-google-genai"),
        google_genai=version("google-genai"),
        head=head,
        plan_sha256=plan_sha256,
        recorded_at=datetime.now(UTC).isoformat(),
    )


def debrief_key(plan: PilotPlan) -> str:
    role = Settings().debrief
    if not isinstance(role, GoogleReply) or role.model != plan.model:
        raise SystemExit("the debrief role is not configured for the planned Google model")
    return require_secret(role.api_key, ConfigKey.DEBRIEF_API_KEY)


def _write(report: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def run_command(arguments: argparse.Namespace) -> int:
    plan = load_plan(arguments.plan)
    approval = ExecutionApproval.model_validate_json(arguments.approval.read_text("utf-8"))
    plan_sha256 = plan_digest(arguments.plan)
    head = git_head()
    problems = approval_problems(approval, plan, plan_sha256, head)
    if problems:
        _write({"refused": "execution approval does not match this run", "problems": problems})
        return 2
    if arguments.cassette.exists():
        _write({"refused": f"{arguments.cassette} exists; a cassette is never overwritten"})
        return 2
    dataset = load_dataset(arguments.dataset)
    secret = debrief_key(plan)
    with arguments.cassette.open("x", encoding="utf-8") as cassette:

        def sink(line: str) -> None:
            cassette.write(line)
            cassette.flush()

        provider = GeminiPilotProvider(
            create_model(plan, secret), plan, provenance_for(plan, plan_sha256, head), sink, secret
        )
        report = replay_report(dataset, plan, provider)
    _write({**report, "ledger": provider.ledger.summary(plan)})
    return 0


def preflight_command(arguments: argparse.Namespace) -> int:
    plan = load_plan(arguments.plan)
    _, calls = pilot_scope(load_dataset(arguments.dataset), plan)
    _write(preflight(plan, [call.request for call in calls]))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals._lineage_live",
        description="Gemini pilot adapter for lineage classification; runs only with an approval",
    )
    parser.add_argument("--dataset", type=Path, default=FIXTURE)
    parser.add_argument("--plan", type=Path, default=PILOT_FIXTURE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "preflight", help="Offline request bounds and worst-case spend"
    ).set_defaults(run=preflight_command)
    run = commands.add_parser("run", help="Call the provider; needs a matching execution approval")
    run.add_argument("--approval", type=Path, required=True)
    run.add_argument("--cassette", type=Path, required=True)
    run.set_defaults(run=run_command)
    arguments = parser.parse_args(argv)
    command: Callable[[argparse.Namespace], int] = arguments.run
    return command(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
