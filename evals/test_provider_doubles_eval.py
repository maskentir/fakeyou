import json
import subprocess
import sys
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from langchain_google_genai.chat_models import GoogleInvalidRequestError

from evals import _provider_doubles as doubles_module
from evals._lineage_classifier import Pricing
from evals._provider_doubles import (
    CASSETTES,
    CassetteWriter,
    ProviderDoubles,
    _token_vector,
    provider_doubles,
    provider_mode,
    recorded_judge,
)
from evals._recording_budget import RecordingBudget, RecordingCaps, RecordingLedger
from evals._replay_cassette import (
    HeardTranscripts,
    ModelExchange,
    ReplayLedger,
    canonical_request,
    model_exchange,
    read_cassette,
    request_digest,
)
from fakeyou.config.settings import Settings
from fakeyou.conversation.turn_judge import JudgeLine, JudgeWindow, ModelTurnJudge
from fakeyou.orchestration.providers import create_turn_judge_model
from fakeyou.perception.inspector import VisualInspection
from fakeyou.perception.interpreter import INSPECTION_PROMPT_MARKER

ROUTE = "/v1beta/models/gemini-3.1-flash-lite:generateContent"
JUDGE = recorded_judge()
RETRIED_STATUSES = (408, 429, 500, 502, 503, 504)
ANSWER = {
    "candidates": [{"content": {"parts": [{"text": "raise_hand"}], "role": "model"}}],
    "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 4, "thoughtsTokenCount": 10},
}
FLASH_LITE = Pricing(
    input_per_million_usd=0.25,
    output_per_million_usd=1.5,
    source="test",
    retrieved="2026-11-21",
    valid_until="2026-12-31",
)


def _budget(folder: Path, max_attempts: int = 10) -> RecordingBudget:
    path = folder / "budget.json"
    ledger = RecordingLedger(
        caps=RecordingCaps(
            max_attempts=max_attempts, max_spend_usd=0.25, max_output_tokens_per_call=65536
        ),
        pricing={"gemini-3.1-flash-lite": FLASH_LITE},
    )
    path.write_text(ledger.model_dump_json(), encoding="utf-8")
    return RecordingBudget.load(path)


def _payload(question: str) -> dict[str, Any]:
    return {"contents": [{"role": "user", "parts": [{"text": question}]}]}


def _recorded(path: Path, question: str) -> Path:
    exchange = model_exchange(
        canonical_request(ROUTE, json.dumps(_payload(question)).encode()),
        200,
        "application/json",
        [(0.0, json.dumps(ANSWER))],
    )
    path.write_text(exchange.model_dump_json() + "\n", encoding="utf-8")
    return path


def _upstream(seen: list[httpx.Request]) -> httpx.AsyncClient:
    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=ANSWER)

    return httpx.AsyncClient(transport=httpx.MockTransport(respond))


async def _ask(doubles: ProviderDoubles, question: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=doubles.app()), base_url="http://double"
    ) as client:
        return await client.post(ROUTE, json=_payload(question))


async def test_record_relays_the_provider_answer_and_keeps_the_canonical_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "scenario.jsonl"
    doubles = ProviderDoubles(
        "record", path, writer=CassetteWriter(path, fresh=True), budget=_budget(tmp_path)
    )
    seen: list[httpx.Request] = []

    async with _upstream(seen) as upstream:
        monkeypatch.setattr(doubles, "_client", upstream)
        response = await _ask(doubles, "Twin, raise your hand")

    [exchange] = read_cassette(path)
    assert response.json() == ANSWER
    assert len(seen) == 1
    assert isinstance(exchange, ModelExchange)
    assert exchange.request == canonical_request(
        ROUTE, json.dumps(_payload("Twin, raise your hand")).encode()
    )


async def test_replay_answers_only_the_recorded_request_and_never_reaches_a_provider(
    tmp_path: Path,
) -> None:
    path = _recorded(tmp_path / "scenario.jsonl", "Twin, raise your hand")
    doubles = ProviderDoubles("replay", path, ledger=ReplayLedger.load(path, JUDGE))

    recorded = await _ask(doubles, "Twin, raise your hand")
    unrecorded = await _ask(doubles, "Twin, lower your hand")
    retried = await _ask(doubles, "Twin, lower your hand")

    assert recorded.json() == ANSWER
    assert unrecorded.status_code == 400
    assert unrecorded.status_code not in RETRIED_STATUSES
    assert "unrecorded_request" in unrecorded.text
    assert "repeated_request" in retried.text
    assert doubles.reaches_providers is False
    report: Any = doubles.report()
    assert [miss["kind"] for miss in report["replay"]["misses"]] == [
        "unrecorded_request",
        "repeated_request",
    ]


async def test_record_missing_replays_what_exists_and_records_only_what_is_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _recorded(tmp_path / "scenario.jsonl", "Twin, raise your hand")
    doubles = ProviderDoubles(
        "record_missing",
        path,
        ledger=ReplayLedger.load(path, JUDGE),
        writer=CassetteWriter(path, fresh=False),
        budget=_budget(tmp_path),
    )
    seen: list[httpx.Request] = []

    async with _upstream(seen) as upstream:
        monkeypatch.setattr(doubles, "_client", upstream)
        await _ask(doubles, "Twin, raise your hand")
        await _ask(doubles, "Twin, lower your hand")

    assert len(seen) == 1
    assert len(read_cassette(path)) == 2
    assert ReplayLedger.load(path, JUDGE).summary(HeardTranscripts())["recorded_exchanges"] == 2


def test_replay_needs_a_cassette_and_names_the_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIMULATOR_PROVIDERS", "replay")
    monkeypatch.setenv("SIMULATOR_CASSETTE_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="sim-absent"):
        provider_doubles("sim-absent")


def test_the_simulator_replays_unless_an_operator_selects_a_provider_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SIMULATOR_PROVIDERS", raising=False)

    assert provider_mode() == "replay"


def _endpoints(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _endpoints(item, f"{path}.{key}")
    elif isinstance(value, str) and "://" in value:
        yield path, value


def test_replay_leaves_no_provider_endpoint_outside_the_doubles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIMULATOR_PROVIDERS", "replay")
    monkeypatch.setenv("SIMULATOR_CASSETTE_DIR", str(tmp_path))
    (tmp_path / "sim-s1.jsonl").write_bytes((CASSETTES / "sim-s1.jsonl").read_bytes())
    doubles = provider_doubles("sim-s1")
    doubles.base_url = "http://127.0.0.1:9"

    settings = doubles.settings(Settings())
    outside = {
        path: url
        for path, url in _endpoints(settings.model_dump(mode="json"))
        if not url.removeprefix("ws").removeprefix("http").startswith("://127.0.0.1:9")
    }

    assert doubles.mode == "replay"
    assert outside == {".memory.store.uri": settings.memory.store.uri}
    assert not doubles.reaches_providers


async def test_a_route_without_a_double_is_refused_and_keeps_the_replay_open(
    tmp_path: Path,
) -> None:
    path = _recorded(tmp_path / "scenario.jsonl", "hello")
    doubles = ProviderDoubles("replay", path, ledger=ReplayLedger.load(path, JUDGE))

    async with doubles.running():
        async with httpx.AsyncClient(base_url=doubles.base_url) as client:
            answer = await client.post("/memory/chat/completions", json={"messages": []})
    replay: Any = doubles.report()["replay"]

    assert answer.status_code == 501
    assert replay["closed"] is False
    assert replay["misses"][0]["route"] == "/memory/chat/completions"


@pytest.mark.parametrize("mode", ["record_looks", "fuzzy", ""])
def test_only_the_supported_provider_modes_are_accepted(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("SIMULATOR_PROVIDERS", mode)

    with pytest.raises(ValueError, match="SIMULATOR_PROVIDERS"):
        provider_mode()


def test_replay_points_every_provider_at_the_doubles_with_a_placeholder_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = tmp_path / "settings.json"
    snapshot.write_text(doubles_module.SNAPSHOT.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(doubles_module, "SNAPSHOT", snapshot)
    path = _recorded(tmp_path / "scenario.jsonl", "hello")
    doubles = ProviderDoubles(
        "replay", path, ledger=ReplayLedger.load(path, JUDGE), base_url="http://127.0.0.1:9"
    )
    settings = doubles.settings(Settings())

    assert settings.reply is not None
    assert str(settings.reply.base_url).startswith("http://127.0.0.1:9")
    assert settings.reply.api_key is not None
    assert settings.reply.api_key.get_secret_value() == "double"
    assert settings.stt.url.startswith("ws://127.0.0.1:9")


def _recorded_looks() -> Iterator[tuple[str, ModelExchange]]:
    for path in sorted(CASSETTES.glob("*.jsonl")):
        for line in read_cassette(path):
            if isinstance(line, ModelExchange) and INSPECTION_PROMPT_MARKER in json.dumps(
                line.request["body"].get("contents", [])[-1:], ensure_ascii=False
            ):
                yield path.stem, line


def test_every_recorded_simulator_look_is_a_structured_inspection() -> None:
    looks = list(_recorded_looks())

    assert {scenario for scenario, _ in looks} == {"sim-s3-camera", "sim-s3-screen"}
    for scenario, exchange in looks:
        body = "".join(text for _delay, text in exchange.chunks)
        text = json.loads(body)["candidates"][0]["content"]["parts"][0]["text"]
        inspection = VisualInspection.model_validate_json(text)
        assert inspection.verdict in {"supported", "not_visible", "unclear"}, scenario


def test_a_free_text_look_is_rejected_by_the_inspection_contract() -> None:
    with pytest.raises(ValueError, match="expected value"):
        VisualInspection.model_validate_json("Harun's shirt is red.")


def test_the_embedding_double_is_deterministic_and_orders_shared_words_first() -> None:
    slide = _token_vector("Beta launch is scheduled for 21 October 2026", 1024)
    brief = _token_vector("The Orchid beta launch is planned for 14 October 2026", 1024)
    other = _token_vector("Quarterly revenue grew in the northern region", 1024)

    assert slide == _token_vector("Beta launch is scheduled for 21 October 2026", 1024)
    related = sum(left * right for left, right in zip(slide, brief, strict=True))
    unrelated = sum(left * right for left, right in zip(slide, other, strict=True))
    assert related > 0.15 > unrelated


async def test_a_provider_error_is_relayed_but_never_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "scenario.jsonl"
    doubles = ProviderDoubles(
        "record", path, writer=CassetteWriter(path, fresh=True), budget=_budget(tmp_path)
    )

    sent: list[httpx.Request] = []

    def busy(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(429, json={"error": {"code": 429}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(busy)) as upstream:
        monkeypatch.setattr(doubles, "_client", upstream)
        response = await _ask(doubles, "Twin, raise your hand")
        retried = await _ask(doubles, "Twin, raise your hand")
    report: Any = doubles.report()

    assert response.status_code == 429
    assert retried.status_code == 400
    assert retried.status_code not in RETRIED_STATUSES
    assert len(sent) == 1
    assert read_cassette(path) == ()
    assert report["provider_errors_not_recorded"] == [
        f"{ROUTE} answered 429",
        f"{ROUTE} not relayed: recording stopped: provider_failure",
    ]
    assert report["recording"]["attempts"] == 1
    assert report["recording"]["failed_attempts"] == 1
    assert report["recording"]["stop_reason"] == "provider_failure"


def test_recording_refuses_to_start_without_a_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIMULATOR_PROVIDERS", "record_missing")
    monkeypatch.setenv("SIMULATOR_CASSETTE_DIR", str(tmp_path))
    monkeypatch.delenv("SIMULATOR_RECORDING_BUDGET", raising=False)
    (tmp_path / "sim-s1.jsonl").write_bytes((CASSETTES / "sim-s1.jsonl").read_bytes())

    with pytest.raises(ValueError, match="SIMULATOR_RECORDING_BUDGET"):
        provider_doubles("sim-s1")


def test_record_missing_refuses_to_start_without_gemini_keys(tmp_path: Path) -> None:
    path = _recorded(tmp_path / "scenario.jsonl", "hello")
    doubles = ProviderDoubles(
        "record_missing",
        path,
        ledger=ReplayLedger.load(path, JUDGE),
        writer=CassetteWriter(path, fresh=False),
        budget=_budget(tmp_path),
    )
    keyless = Settings().model_copy(update={"reply": None, "reply_strong": None, "debrief": None})
    keyless = keyless.model_copy(
        update={
            "perception": keyless.perception.model_copy(
                update={
                    "interpreter": keyless.perception.interpreter.model_copy(
                        update={"api_key": None}
                    )
                }
            )
        }
    )

    with pytest.raises(ValueError, match="needs Gemini keys"):
        doubles.settings(keyless)


async def test_record_missing_keeps_only_what_the_run_used_and_what_it_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "scenario.jsonl"
    stale = model_exchange(
        canonical_request(ROUTE, json.dumps(_payload("Twin, stale question")).encode()),
        200,
        "application/json",
        [(0.0, json.dumps(ANSWER))],
    )
    _recorded(path, "Twin, raise your hand")
    with path.open("a", encoding="utf-8") as file:
        file.write(stale.model_dump_json() + "\n")
    doubles = ProviderDoubles(
        "record_missing",
        path,
        ledger=ReplayLedger.load(path, JUDGE),
        writer=CassetteWriter(path, fresh=False),
        budget=_budget(tmp_path),
    )
    seen: list[httpx.Request] = []

    async with _upstream(seen) as upstream:
        monkeypatch.setattr(doubles, "_client", upstream)
        await _ask(doubles, "Twin, raise your hand")
        await _ask(doubles, "Twin, lower your hand")
    doubles.finish_recording()

    kept = [line for line in read_cassette(path) if isinstance(line, ModelExchange)]
    asked = {json.dumps(line.request["body"]) for line in kept}
    assert len(kept) == 2
    assert not any("stale" in question for question in asked)


CAPTURE_AFTER = (
    "import importlib, asyncio, json, sys; importlib.import_module(sys.argv[2]); "
    "from evals.test_provider_doubles_eval import captured_judge_request; "
    "json.dump(asyncio.run(captured_judge_request(sys.argv[1])), sys.stdout)"
)
FIRST_IMPORTS = {
    "production_worker": "fakeyou.worker.__main__",
    "production_cli": "fakeyou.cli.main",
    "simulator": "evals._meeting_simulator",
    "session_runtime": "fakeyou.orchestration.runtime",
    "language_first": "fakeyou.foundation.language",
}
CAPTURED_LATEST = "Rani Twin, can you"


async def captured_judge_request(folder: str) -> dict[str, Any]:
    path = Path(folder) / "scenario.jsonl"
    path.write_text("", encoding="utf-8")
    doubles = ProviderDoubles("replay", path, ledger=ReplayLedger.load(path, JUDGE))
    asked: list[dict[str, Any]] = []
    respond = ReplayLedger.respond

    def capture(ledger: ReplayLedger, route: str, body: bytes) -> object:
        asked.append(canonical_request(route, body))
        return respond(ledger, route, body)

    with patch.object(ReplayLedger, "respond", capture):
        async with doubles.running():
            model = create_turn_judge_model(doubles.settings(Settings()))
            judge = ModelTurnJudge(model, twin_names=("Rani Twin",), timeout_seconds=10.0)
            with suppress(GoogleInvalidRequestError):
                await judge.judge(JudgeWindow((), JudgeLine("Speaker A", CAPTURED_LATEST)))
    [request] = asked
    return request


def _captured_after(first_import: str, folder: Path) -> dict[str, Any]:
    folder.mkdir()
    captured = subprocess.run(
        [sys.executable, "-c", CAPTURE_AFTER, str(folder), first_import],
        capture_output=True,
        check=True,
        text=True,
        cwd=Path(__file__).parent.parent,
    )
    request: dict[str, Any] = json.loads(captured.stdout)
    return request


def test_every_process_sends_the_recorded_judge_request_whatever_it_imports_first(
    tmp_path: Path,
) -> None:
    frames = {
        name: _captured_after(module, tmp_path / name) for name, module in FIRST_IMPORTS.items()
    }

    assert len({request_digest(request) for request in frames.values()}) == 1
    assert all(JUDGE.latest(request) == CAPTURED_LATEST for request in frames.values())
