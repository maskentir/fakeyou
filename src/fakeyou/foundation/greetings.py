import json
from typing import Any, override

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage

from fakeyou.foundation.resources import read_package_text
from fakeyou.foundation.triggers import trigger_kind

_VOCABULARY: dict[str, Any] = json.loads(read_package_text("greetings_vocabulary.json"))
_OUTPUTS: dict[str, str] = _VOCABULARY["outputs"]
_GREETINGS: set[str] = set(_VOCABULARY["greetings"])
_LEAD_INS: tuple[tuple[str, ...], ...] = tuple(
    sorted((tuple(item) for item in _VOCABULARY["lead_ins"]), key=len, reverse=True)
)
_TRAILERS: tuple[tuple[str, ...], ...] = tuple(
    sorted((tuple(item) for item in _VOCABULARY["trailers"]), key=len, reverse=True)
)
_BANNED_CLAIM_WORDS: set[str] = set(_VOCABULARY["banned_claim_words"])
_EDGE_PUNCTUATION = '.,!;:"“”‘’()[]{}-–—…'


def _tokens(text: str) -> list[str] | None:
    if "?" in text:
        return None
    words = [word.strip(_EDGE_PUNCTUATION) for word in text.lower().split()]
    return [word for word in words if word]


def _is_name_token(token: str) -> bool:
    core = token.strip("-'")
    return len(core) > 0 and all(char.isalpha() or char in "-'" for char in core)


def _valid_claim(claim: list[str]) -> bool:
    return (
        1 <= len(claim) <= 5
        and all(_is_name_token(token) for token in claim)
        and all(token not in _BANNED_CLAIM_WORDS for token in claim)
    )


def neutral_greeting(text: str, language: str) -> str | None:
    output = _OUTPUTS.get(language)
    if output is None:
        return None
    tokens = _tokens(text)
    if not tokens or tokens[0] not in _GREETINGS:
        return None
    rest = tokens[1:]
    if not rest:
        return output
    for lead in _LEAD_INS:
        if tuple(rest[: len(lead)]) == lead:
            return output if _valid_claim(rest[len(lead) :]) else None
    for trailer in _TRAILERS:
        if len(rest) > len(trailer) and tuple(rest[-len(trailer) :]) == trailer:
            return output if _valid_claim(rest[: -len(trailer)]) else None
    return None


class _GreetingMiddleware(AgentMiddleware[Any, Any, Any]):
    def __init__(self, language: str) -> None:
        self._language = language

    @hook_config(can_jump_to=["end"])
    @override
    def before_agent(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        trailing: list[HumanMessage] = []
        for message in reversed(state.get("messages", [])):
            if not isinstance(message, HumanMessage):
                break
            trailing.append(message)
        if len(trailing) != 1:
            return None
        latest = trailing[0]
        if trigger_kind(latest) != "speech":
            return None
        reply = neutral_greeting(latest.text, self._language)
        if reply is None:
            return None
        return {"messages": [AIMessage(content=reply)], "jump_to": "end"}


def create_greeting_middleware(language: str) -> AgentMiddleware[Any, Any, Any]:
    return _GreetingMiddleware(language)
