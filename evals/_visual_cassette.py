from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel

from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.perception.budget import estimate_image_tokens
from fakeyou.perception.inspector import VISUAL_INSPECTION_SCHEMA_VERSION, VisualInspection
from fakeyou.perception.interpreter import (
    CAMERA_EVAL_SOURCE,
    SCREEN_SOURCE,
    image_payload,
    inspection_message,
    structured_parts,
)

CASSETTE = Path(__file__).parent / "fixtures" / "visual-inspection" / "provider-cassette.jsonl"
LEDGER = Path(__file__).parent / "fixtures" / "visual-inspection" / "experiment-attempts.jsonl"
PROVIDER = "google"
MODEL = "gemini-3.8-flash"
INPUT_USD_PER_MTOK = 0.75
OUTPUT_USD_PER_MTOK = 3.75
MAX_REQUESTS = 12
MAX_USD = 1.0
PREFLIGHT_OUTPUT_TOKENS = 200


def source_for(image: str) -> str:
    if image.startswith("schematic-camera"):
        return CAMERA_EVAL_SOURCE
    return SCREEN_SOURCE


def image_digest(image: bytes) -> str:
    return hashlib.sha256(image).hexdigest()


def raw_text(raw: Any) -> str:
    content = getattr(raw, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


def usd_for_usage(usage: dict[str, Any]) -> float:
    prompt = int(usage.get("input_tokens") or 0)
    output = int(usage.get("output_tokens") or 0)
    return (prompt * INPUT_USD_PER_MTOK + output * OUTPUT_USD_PER_MTOK) / 1_000_000


def preflight_usd(width: int, height: int) -> float:
    estimate = estimate_image_tokens(width, height)
    return (
        estimate * INPUT_USD_PER_MTOK + PREFLIGHT_OUTPUT_TOKENS * OUTPUT_USD_PER_MTOK
    ) / 1_000_000


def load_ledger(path: Path = LEDGER) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def append_ledger(entry: dict[str, Any], path: Path = LEDGER) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_ledger(entries: Sequence[dict[str, Any]], path: Path = LEDGER) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )
    temporary.replace(path)


def load_cassette(path: Path = CASSETTE) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def match_entry(
    entries: Sequence[dict[str, Any]], image_sha256: str, question: str
) -> dict[str, Any] | None:
    for entry in entries:
        if entry.get("image_sha256") == image_sha256 and entry.get("question") == question:
            return entry
    return None


def cassette_inspector(path: Path = CASSETTE) -> Callable[[bytes, str], Awaitable[str]]:
    entries = load_cassette(path)

    async def inspect(image: bytes, question: str) -> str:
        entry = match_entry(entries, image_digest(image), question)
        if entry is None:
            raise LookupError(f"no recorded response for {image_digest(image)[:12]} {question!r}")
        return str(entry["text"])

    return inspect


class ExperimentBudget:
    def __init__(
        self,
        *,
        max_requests: int = MAX_REQUESTS,
        max_usd: float = MAX_USD,
        ledger: Path = LEDGER,
    ) -> None:
        self.max_requests = max_requests
        self.max_usd = max_usd
        self.ledger = ledger
        spent = load_ledger(ledger)
        self.attempts = len(spent)
        self.inference_attempts = sum(1 for entry in spent if entry.get("inference") is True)
        self.failed = sum(1 for entry in spent if entry.get("status") == "failed")
        self.spent_usd = sum(float(entry.get("usd") or 0.0) for entry in spent)

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_requests or self.spent_usd >= self.max_usd

    def allows(self, estimate_usd: float) -> bool:
        return not self.exhausted and self.spent_usd + estimate_usd <= self.max_usd

    def reserve(self, attempt: dict[str, Any]) -> str:
        attempt_id = str(uuid4())
        entry = {
            "status": "reserved",
            "id": attempt_id,
            "provider": PROVIDER,
            "model": MODEL,
            "usd": 0.0,
            "inference": False,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **attempt,
        }
        append_ledger(entry, self.ledger)
        self.attempts += 1
        return attempt_id

    def settle(
        self,
        attempt_id: str,
        *,
        status: str = "recorded",
        usage: dict[str, Any] | None = None,
        error: str | None = None,
        inference: bool = True,
    ) -> float:
        cost = usd_for_usage(usage or {})
        entries = load_ledger(self.ledger)
        for entry in entries:
            if entry.get("id") != attempt_id:
                continue
            entry["status"] = status
            entry["usd"] = round(cost, 6)
            entry["inference"] = inference
            if usage:
                entry["usage"] = usage
            if error is not None:
                entry["error"] = error
            write_ledger(entries, self.ledger)
            break
        if inference:
            self.inference_attempts += 1
        if status == "failed":
            self.failed += 1
        else:
            self.spent_usd += cost
        return cost

    def record(
        self,
        attempt: dict[str, Any],
        *,
        status: str = "recorded",
        usage: dict[str, Any] | None = None,
        error: str | None = None,
        inference: bool = True,
    ) -> float:
        attempt_id = self.reserve(attempt)
        return self.settle(attempt_id, status=status, usage=usage, error=error, inference=inference)


async def record_visual_cassette(
    cases: Sequence[tuple[str, str, bytes]],
    model: BaseChatModel,
    *,
    path: Path = CASSETTE,
    budget: ExperimentBudget | None = None,
) -> list[dict[str, Any]]:
    guard = budget or ExperimentBudget()
    existing = load_cassette(path)
    recorded: list[dict[str, Any]] = []
    for image, question, data in cases:
        if guard.exhausted:
            break
        digest = image_digest(data)
        if match_entry(existing, digest, question) is not None:
            continue
        payload = await asyncio.to_thread(image_payload, data)
        estimate = preflight_usd(payload.width, payload.height)
        if not guard.allows(estimate):
            break
        attempt = {"source": "eval", "image_sha256": digest, "question": question}
        attempt_id = guard.reserve(attempt)
        try:
            message = await inspection_message(data, question, source=source_for(image))
            structured = model.with_structured_output(VisualInspection, include_raw=True)
            started = time.monotonic()
            result = await structured.ainvoke([message])
            latency_ms = round((time.monotonic() - started) * 1000)
            raw, _parsed = structured_parts(result)
        except (TimeoutError, OSError, ProviderFault) as error:
            guard.settle(attempt_id, status="failed", error=type(error).__name__, inference=True)
            sys.stdout.write(f"failed {question!r}: {type(error).__name__}: {error}\n")
            continue
        usage = dict(getattr(raw, "usage_metadata", None) or {})
        cost = guard.settle(attempt_id, usage=usage)
        entry = {
            "image_sha256": digest,
            "question": question,
            "provider": PROVIDER,
            "model": MODEL,
            "schema_version": VISUAL_INSPECTION_SCHEMA_VERSION,
            "usage": usage,
            "usd": round(cost, 6),
            "latency_ms": latency_ms,
            "text": raw_text(raw),
        }
        recorded.append(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return recorded


async def _main() -> None:
    from evals._visual_inspection import load_visual_fixture
    from fakeyou.config.settings import Settings
    from fakeyou.orchestration.providers import create_perception_chat_model

    fixture = load_visual_fixture()
    model = create_perception_chat_model(Settings())
    cases = [
        (case.image, case.question, (fixture.image_root / case.image).read_bytes())
        for case in fixture.cases
    ]
    budget = ExperimentBudget()
    recorded = await record_visual_cassette(cases, model, budget=budget)
    sys.stdout.write(
        json.dumps(
            {
                "outbound_attempts": budget.attempts,
                "inference_attempts": budget.inference_attempts,
                "failed": budget.failed,
                "cost_estimated_from_successful_usage_usd": round(budget.spent_usd, 6),
                "recorded_now": len(recorded),
                "cases": [
                    {
                        "question": entry["question"],
                        "latency_ms": entry["latency_ms"],
                        "usd": entry["usd"],
                    }
                    for entry in recorded
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
