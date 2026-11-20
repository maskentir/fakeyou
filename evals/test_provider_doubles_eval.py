import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals import _provider_doubles as doubles_module
from evals._provider_doubles import (
    CASSETTES,
    CassetteWriter,
    ProviderDoubles,
    _token_vector,
    provider_doubles,
    provider_mode,
)
from evals._replay_cassette import (
    HeardTranscripts,
    ModelExchange,
    ReplayLedger,
    canonical_request,
    model_exchange,
    read_cassette,
)
from fakeyou.config.settings import Settings
from fakeyou.perception.inspector import VisualInspection
from fakeyou.perception.interpreter import INSPECTION_PROMPT_MARKER

ROUTE = "/v1beta/models/gemini-3.1-flash-lite:generateContent"
ANSWER = {"candidates": [{"content": {"parts": [{"text": "raise_hand"}], "role": "model"}}]}


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
    doubles = ProviderDoubles("record", path, writer=CassetteWriter(path, fresh=True))
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
    doubles = ProviderDoubles("replay", path, ledger=ReplayLedger.load(path))

    recorded = await _ask(doubles, "Twin, raise your hand")
    unrecorded = await _ask(doubles, "Twin, lower your hand")

    assert recorded.json() == ANSWER
    assert unrecorded.status_code == 500
    assert "unrecorded_request" in unrecorded.text
    assert doubles.reaches_providers is False
    report: Any = doubles.report()
    assert [miss["kind"] for miss in report["replay"]["misses"]] == ["unrecorded_request"]


async def test_record_missing_replays_what_exists_and_records_only_what_is_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _recorded(tmp_path / "scenario.jsonl", "Twin, raise your hand")
    doubles = ProviderDoubles(
        "record_missing",
        path,
        ledger=ReplayLedger.load(path),
        writer=CassetteWriter(path, fresh=False),
    )
    seen: list[httpx.Request] = []

    async with _upstream(seen) as upstream:
        monkeypatch.setattr(doubles, "_client", upstream)
        await _ask(doubles, "Twin, raise your hand")
        await _ask(doubles, "Twin, lower your hand")

    assert len(seen) == 1
    assert len(read_cassette(path)) == 2
    assert ReplayLedger.load(path).summary(HeardTranscripts())["recorded_exchanges"] == 2


def test_replay_needs_a_cassette_and_names_the_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIMULATOR_PROVIDERS", "replay")
    monkeypatch.setenv("SIMULATOR_CASSETTE_DIR", str(tmp_path))

    with pytest.raises(ValueError, match="sim-absent"):
        provider_doubles("sim-absent")


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
        "replay", path, ledger=ReplayLedger.load(path), base_url="http://127.0.0.1:9"
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
    doubles = ProviderDoubles("record", path, writer=CassetteWriter(path, fresh=True))

    def busy(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"code": 429}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(busy)) as upstream:
        monkeypatch.setattr(doubles, "_client", upstream)
        response = await _ask(doubles, "Twin, raise your hand")

    assert response.status_code == 429
    assert read_cassette(path) == ()
    assert doubles.report()["provider_errors_not_recorded"] == [f"{ROUTE} answered 429"]


def test_record_missing_refuses_to_start_without_gemini_keys(tmp_path: Path) -> None:
    path = _recorded(tmp_path / "scenario.jsonl", "hello")
    doubles = ProviderDoubles(
        "record_missing",
        path,
        ledger=ReplayLedger.load(path),
        writer=CassetteWriter(path, fresh=False),
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
        ledger=ReplayLedger.load(path),
        writer=CassetteWriter(path, fresh=False),
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
