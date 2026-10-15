import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import httpx2

TurnAction = Literal["speak", "silent", "clarify"]


@dataclass(frozen=True, slots=True)
class TurnDecision:
    action: TurnAction
    confidence: float
    addressed_p: float
    conversing_p: float
    clarify_p: float


@runtime_checkable
class ParticipationDecider(Protocol):
    async def decide(self, state_text: str) -> TurnDecision: ...
    async def aclose(self) -> None: ...


def _silent() -> TurnDecision:
    return TurnDecision("silent", 0.0, 0.0, 0.0, 0.0)


def build_jev_payload(state_text: str, model: str) -> dict[str, Any]:
    return {
        "model": model,
        "state": state_text,
        "questions": {
            "decision": {"type": "choice", "options": ["speak", "silent", "clarify"]},
            "addressed_to_me": {
                "type": "noul",
                "instructions": "The last utterance is addressed to the twin",
            },
            "others_conversing": {
                "type": "noul",
                "instructions": "This is part of a conversation between two other people",
            },
            "needs_clarify": {
                "type": "noul",
                "instructions": "The referent is ambiguous and clarification is warranted",
            },
        },
    }


def parse_jev_response(body: dict[str, Any]) -> TurnDecision:
    decision = body.get("decision") if isinstance(body, dict) else None
    if not isinstance(decision, dict):
        return _silent()
    action = decision.get("choice")
    if action not in ("speak", "silent", "clarify"):
        return _silent()
    confidence = decision.get("confidence", 0.0)

    def _prob(key: str) -> float:
        node = body.get(key)
        value = node.get("probability") if isinstance(node, dict) else None
        return value if isinstance(value, (int, float)) else 0.0

    return TurnDecision(
        action,
        float(confidence),
        _prob("addressed_to_me"),
        _prob("others_conversing"),
        _prob("needs_clarify"),
    )


class JevDecider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float,
        transport: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._payload_model = model
        self._url = base_url.rstrip("/") + "/v1/systemone"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout_seconds
        self._transport = transport
        self._client = httpx2.AsyncClient(timeout=timeout_seconds) if transport is None else None

    async def decide(self, state_text: str) -> TurnDecision:
        payload = build_jev_payload(state_text, self._payload_model)
        try:
            if self._transport is not None:
                body = await self._transport(payload)
            else:
                assert self._client is not None
                response = await self._client.post(self._url, headers=self._headers, json=payload)
                response.raise_for_status()
                body = response.json()
        except (httpx2.HTTPError, TimeoutError, ValueError):
            return _silent()
        try:
            return parse_jev_response(body)
        except (ValueError, TypeError, AttributeError):
            return _silent()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


_GEMINI_PROMPT = (
    "Decide whether the twin joins the floor. "
    "State lists recent turns as speaker: text plus a turn cue line. "
    "Answer with one decision: speak when the last turn calls the twin by cue, "
    "clarify when the target is unclear and nobody else holds the floor, "
    "silent in all other cases including talk between two other people. "
    "State:\n"
)

_GEMINI_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "decision": {"type": "STRING", "enum": ["speak", "silent", "clarify"]},
        "confidence": {"type": "NUMBER"},
        "addressed_to_me": {"type": "NUMBER"},
        "others_conversing": {"type": "NUMBER"},
        "needs_clarify": {"type": "NUMBER"},
    },
    "required": ["decision", "confidence"],
}


def build_gemini_payload(state_text: str, model: str) -> tuple[str, dict[str, Any]]:
    return (
        f"{model}:generateContent",
        {
            "contents": [{"parts": [{"text": _GEMINI_PROMPT + state_text}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": _GEMINI_SCHEMA,
            },
        },
    )


def _gemini_text(body: dict[str, Any]) -> str | None:
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    first = candidates[0]
    content = first.get("content") if isinstance(first, dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list) or not parts:
        return None
    part = parts[0]
    text = part.get("text") if isinstance(part, dict) else None
    return text if isinstance(text, str) else None


def parse_gemini_response(body: dict[str, Any]) -> TurnDecision:
    text = _gemini_text(body)
    if text is None:
        return _silent()
    try:
        parsed = json.loads(text)
    except ValueError:
        return _silent()
    if not isinstance(parsed, dict):
        return _silent()
    action = parsed.get("decision")
    if action not in ("speak", "silent", "clarify"):
        return _silent()

    def _number(key: str) -> float:
        value = parsed.get(key)
        return float(value) if isinstance(value, (int, float)) else 0.0

    return TurnDecision(
        action,
        _number("confidence"),
        _number("addressed_to_me"),
        _number("others_conversing"),
        _number("needs_clarify"),
    )


class GeminiDecider:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float,
        transport: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self._payload_model = model
        self._url = base_url.rstrip("/") + "/v1beta/models"
        self._headers = {"x-goog-api-key": api_key}
        self._timeout = timeout_seconds
        self._transport = transport
        self._client = httpx2.AsyncClient(timeout=timeout_seconds) if transport is None else None

    async def decide(self, state_text: str) -> TurnDecision:
        path, payload = build_gemini_payload(state_text, self._payload_model)
        try:
            if self._transport is not None:
                body = await self._transport(payload)
            else:
                assert self._client is not None
                response = await self._client.post(
                    f"{self._url}/{path}", headers=self._headers, json=payload
                )
                response.raise_for_status()
                body = response.json()
        except (httpx2.HTTPError, TimeoutError, ValueError):
            return _silent()
        try:
            return parse_gemini_response(body)
        except (ValueError, TypeError, AttributeError):
            return _silent()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
