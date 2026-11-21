import asyncio
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
import websockets
from langchain_google_genai.chat_models import GoogleInvalidRequestError
from pydantic import SecretStr

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
    speech_recording,
)
from evals._recording_budget import RecordingBudget, RecordingCaps, RecordingLedger
from evals._replay_cassette import (
    HeardTranscripts,
    ModelExchange,
    ReplayLedger,
    SpeechMessage,
    canonical_request,
    model_exchange,
    read_cassette,
    request_digest,
)
from evals._speech_recording import SpeechRecording
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


SPEECH_CLIP = "review-token.wav"
LIVE_MUSE_KEY = "muse-live-key-never-written"
LOUD = b"\x40\x1f" * 320
QUIET = bytes(640)
SPOKEN = " Rani Twin, what's the review token printed below the launch date? "


class FakeMuse:
    def __init__(
        self, *, complete: bool = True, close_after_audio: bool = False, silent: bool = False
    ) -> None:
        self.complete = complete
        self.close_after_audio = close_after_audio
        self.silent = silent
        self.handshakes: list[dict[str, Any]] = []
        self.audio_bytes = 0
        self.sent: list[dict[str, Any]] = []

    async def say(self, connection: websockets.ServerConnection, message: dict[str, Any]) -> None:
        self.sent.append(message)
        await connection.send(json.dumps(message))

    async def handler(self, connection: websockets.ServerConnection) -> None:
        self.handshakes.append(json.loads(await connection.recv()))
        await connection.send(json.dumps({"sessionId": "muse-session-1"}))
        started = ended = False
        async for raw in connection:
            if isinstance(raw, str):
                if '"endStream"' in raw:
                    await connection.close()
                    return
                continue
            self.audio_bytes += len(raw)
            if self.close_after_audio:
                await connection.close(code=1011)
                return
            if self.silent:
                continue
            if raw != QUIET and not started:
                started = True
                await self.say(connection, {"type": "speechStart", "turnId": 3})
                await self.say(
                    connection, {"type": "transcript", "transcript": " Rani Twin,", "final": False}
                )
                await self.say(connection, {"type": "speaker", "label": "A"})
            elif raw == QUIET and started and not ended:
                ended = True
                await self.say(connection, {"type": "speechEnd", "turnId": 3})
                if self.complete:
                    await self.say(
                        connection, {"type": "speechComplete", "turnId": 3, "transcript": SPOKEN}
                    )


async def speak_through(
    doubles: ProviderDoubles, clips: list[str], *, hold: float = 2.0
) -> list[dict[str, Any]]:
    received: list[dict[str, Any]] = []
    url = doubles.base_url.replace("http://", "ws://") + "/stt?sessionId=worker-1"
    async with websockets.connect(url) as stt:
        handshake = {
            "authorization": {"accessToken": "Bearer double"},
            "audioEncoding": "PCM_16KHZ",
            "model": "muse-voice-transcribe-1.0",
        }
        with suppress(websockets.ConnectionClosed, TimeoutError):
            await stt.send(json.dumps(handshake))
            received.append(json.loads(await stt.recv()))
            for clip in clips:
                doubles.script(clip)
                for chunk in (LOUD, LOUD, LOUD, QUIET, QUIET):
                    await stt.send(chunk)
            async with asyncio.timeout(hold):
                async for raw in stt:
                    received.append(json.loads(raw))
                    if received[-1].get("type") == "speechComplete":
                        break
        with suppress(websockets.ConnectionClosed, TimeoutError):
            await stt.send(json.dumps({"type": "endStream"}))
    return received


def speech_doubles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    muse_url: str,
    speech: SpeechRecording | None = None,
) -> ProviderDoubles:
    monkeypatch.setattr(doubles_module, "QUIET_GAP_SECONDS", 0.0)
    path = tmp_path / "sim-s3-screen.jsonl"
    path.write_text("", encoding="utf-8")
    doubles = ProviderDoubles(
        "record_missing",
        path,
        ledger=ReplayLedger.load(path, JUDGE),
        writer=CassetteWriter(path, fresh=False),
        budget=_budget(tmp_path),
        speech=speech or SpeechRecording(frozenset({SPEECH_CLIP})),
        stt_upstream_url=muse_url,
    )
    monkeypatch.setattr(doubles, "_stt_key", SecretStr(LIVE_MUSE_KEY))
    return doubles


async def record_speech(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    muse: FakeMuse,
    clips: list[str],
    speech: SpeechRecording | None = None,
) -> tuple[ProviderDoubles, list[dict[str, Any]]]:
    async with websockets.serve(muse.handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        doubles = speech_doubles(tmp_path, monkeypatch, f"ws://127.0.0.1:{port}", speech)
        async with doubles.running():
            received = await speak_through(doubles, clips)
    return doubles, received


def speech_lines(path: Path) -> list[SpeechMessage]:
    return [line for line in read_cassette(path) if isinstance(line, SpeechMessage)]


async def test_an_allowed_clip_is_relayed_to_muse_and_recorded_faithfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = doubles_module.SNAPSHOT.read_bytes()
    muse = FakeMuse()

    doubles, received = await record_speech(tmp_path, monkeypatch, muse, [SPEECH_CLIP])
    lines = speech_lines(doubles.cassette)
    report: Any = doubles.report()

    assert received[0] == {"sessionId": "muse-session-1"}
    assert received[-1]["transcript"] == SPOKEN
    assert muse.handshakes[0]["authorization"] == {"accessToken": f"Bearer {LIVE_MUSE_KEY}"}
    assert {(line.clip, line.take) for line in lines} == {(SPEECH_CLIP, 1)}
    assert [line.message for line in lines if line.message["type"] != "simulatorVoiceEnd"] == (
        muse.sent
    )
    assert "simulatorVoiceEnd" in [line.message["type"] for line in lines]
    assert report["speech_recording"]["sessions"] == 1
    assert report["speech_recording"]["pcm_bytes"] == muse.audio_bytes
    assert report["speech_recording"]["stop_reason"] is None
    assert report["provider_errors_not_recorded"] == []
    assert LIVE_MUSE_KEY not in doubles.cassette.read_text(encoding="utf-8")
    assert LIVE_MUSE_KEY not in json.dumps(report)
    assert doubles_module.SNAPSHOT.read_bytes() == snapshot


async def test_strict_replay_consumes_the_newly_recorded_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded, _received = await record_speech(tmp_path, monkeypatch, FakeMuse(), [SPEECH_CLIP])
    replay = ProviderDoubles(
        "replay", recorded.cassette, ledger=ReplayLedger.load(recorded.cassette, JUDGE)
    )

    async with replay.running():
        received = await speak_through(replay, [SPEECH_CLIP])
    summary: Any = replay.report()["replay"]

    assert received[-1] == {"type": "speechComplete", "turnId": 3, "transcript": SPOKEN}
    assert (summary["heard_takes"], summary["recorded_takes"]) == (1, 1)
    assert summary["closed"] is True


UNTRUSTED = {
    "unexpected_clip": (["beta-question.wav"], FakeMuse(), None),
    "additional_take": ([SPEECH_CLIP, SPEECH_CLIP], FakeMuse(), None),
    "audio_limit": (
        [SPEECH_CLIP],
        FakeMuse(),
        SpeechRecording(frozenset({SPEECH_CLIP}), max_audio_seconds=0.01),
    ),
    "deadline": (
        [],
        FakeMuse(silent=True),
        SpeechRecording(frozenset({SPEECH_CLIP}), max_wall_seconds=0.2),
    ),
    "provider_closed": ([SPEECH_CLIP], FakeMuse(close_after_audio=True), None),
    "incomplete_transcript": ([SPEECH_CLIP], FakeMuse(complete=False), None),
}


@pytest.mark.parametrize("reason", list(UNTRUSTED))
async def test_a_speech_recording_that_cannot_be_trusted_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    clips, muse, speech = UNTRUSTED[reason]

    doubles, _received = await record_speech(tmp_path, monkeypatch, muse, clips, speech)
    report: Any = doubles.report()

    assert report["speech_recording"]["stop_reason"] == reason
    assert report["provider_errors_not_recorded"] == [f"speech not recorded: {reason}"]
    assert speech_lines(doubles.cassette) == []


async def test_without_a_speech_allowlist_recorded_clips_still_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded, _received = await record_speech(tmp_path, monkeypatch, FakeMuse(), [SPEECH_CLIP])
    doubles = ProviderDoubles(
        "record_missing",
        recorded.cassette,
        ledger=ReplayLedger.load(recorded.cassette, JUDGE),
        writer=CassetteWriter(recorded.cassette, fresh=False),
        budget=_budget(tmp_path),
        stt_upstream_url="ws://127.0.0.1:9",
    )

    async with doubles.running():
        received = await speak_through(doubles, [SPEECH_CLIP])

    assert received[-1]["transcript"] == SPOKEN
    assert doubles.report()["speech_recording"] is None


def test_an_allowlisted_clip_that_already_has_takes_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "scenario.jsonl"
    path.write_text(
        SpeechMessage(
            kind="speech", clip=SPEECH_CLIP, take=1, offset=0.1, message={"type": "speechStart"}
        ).model_dump_json()
        + "\n",
        encoding="utf-8",
    )

    with patch.dict("os.environ", {"SIMULATOR_RECORD_SPEECH": SPEECH_CLIP}):
        with pytest.raises(ValueError, match="already have takes"):
            speech_recording(ReplayLedger.load(path, JUDGE))
