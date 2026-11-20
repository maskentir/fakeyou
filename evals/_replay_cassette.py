import base64
import binascii
import difflib
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError

_STRICT = ConfigDict(extra="forbid", frozen=True)
_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
DIFF_CONTEXT = 80


@dataclass(frozen=True, slots=True)
class VolatileRule:
    name: str
    pattern: re.Pattern[str]
    placeholder: str


VOLATILE_RULES: tuple[VolatileRule, ...] = (
    VolatileRule(
        "vision_latency_ms",
        re.compile(r'("vision_latency_ms\\*"\s*:\s*)\d+'),
        r"\1<measured>",
    ),
    VolatileRule(
        "seconds_ago",
        re.compile(r'("seconds_ago\\*"\s*:\s*)\d+'),
        r"\1<elapsed>",
    ),
)


class CassetteError(ValueError):
    pass


def _image(part: Mapping[str, Any]) -> dict[str, str]:
    data = part.get("data")
    if not isinstance(data, str):
        raise CassetteError("inlineData without base64 data")
    alphabet = b"-_" if any(mark in data for mark in "-_") else None
    try:
        decoded = base64.b64decode(data, altchars=alphabet, validate=True)
    except (binascii.Error, ValueError) as error:
        raise CassetteError("inlineData is not valid base64") from error
    return {
        "mimeType": str(part.get("mimeType", "")),
        "sha256": hashlib.sha256(decoded).hexdigest(),
    }


def _text(value: str, rules: Sequence[VolatileRule]) -> str:
    for rule in rules:
        value = rule.pattern.sub(rule.placeholder, value)
    return value


def _canonical(value: Any, rules: Sequence[VolatileRule]) -> Any:
    if isinstance(value, dict):
        return {
            key: _image(item)
            if key == "inlineData" and isinstance(item, dict)
            else _canonical(item, rules)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_canonical(item, rules) for item in value]
    if isinstance(value, str):
        return _text(value, rules)
    return value


def _with_rules(value: Any, rules: Sequence[VolatileRule]) -> Any:
    if isinstance(value, dict):
        return {key: _with_rules(item, rules) for key, item in value.items()}
    if isinstance(value, list):
        return [_with_rules(item, rules) for item in value]
    if isinstance(value, str):
        return _text(value, rules)
    return value


def matching_digest(request: Mapping[str, Any], rules: Sequence[VolatileRule]) -> str:
    return request_digest(_with_rules(dict(request), rules))


def canonical_request(
    route: str, body: bytes, rules: Sequence[VolatileRule] = VOLATILE_RULES
) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except ValueError as error:
        raise CassetteError(f"request to {route} is not JSON") from error
    if not isinstance(parsed, dict):
        raise CassetteError(f"request to {route} is not a JSON object")
    return {"route": route, "body": _canonical(parsed, rules)}


def request_digest(request: Mapping[str, Any]) -> str:
    canonical = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ModelExchange(BaseModel):
    model_config = _STRICT

    kind: Literal["model"]
    request_sha256: _Digest
    request: dict[str, Any]
    status: int
    media_type: str
    chunks: tuple[tuple[float, str], ...]

    @property
    def route(self) -> str:
        return str(self.request["route"])


class SpeechMessage(BaseModel):
    model_config = _STRICT

    kind: Literal["speech"]
    clip: Annotated[str, StringConstraints(min_length=1)]
    take: Annotated[int, Field(ge=1)]
    offset: float
    message: dict[str, Any]


CassetteLine = Annotated[ModelExchange | SpeechMessage, Field(discriminator="kind")]
_LINE: TypeAdapter[ModelExchange | SpeechMessage] = TypeAdapter(CassetteLine)


def _validated(number: int, raw: str) -> ModelExchange | SpeechMessage:
    try:
        line = _LINE.validate_json(raw)
    except ValidationError as error:
        raise CassetteError(
            f"line {number} does not follow the cassette contract: {error}"
        ) from error
    if isinstance(line, ModelExchange) and request_digest(line.request) != line.request_sha256:
        raise CassetteError(
            f"line {number} carries a request digest that does not match its request"
        )
    return line


def read_cassette(path: Path) -> tuple[ModelExchange | SpeechMessage, ...]:
    if not path.exists():
        raise CassetteError(f"no cassette at {path}; record it before replaying")
    lines = path.read_text(encoding="utf-8").splitlines()
    return tuple(_validated(number, raw) for number, raw in enumerate(lines, 1) if raw.strip())


def model_exchange(
    request: Mapping[str, Any], status: int, media_type: str, chunks: Iterable[tuple[float, str]]
) -> ModelExchange:
    return ModelExchange(
        kind="model",
        request_sha256=request_digest(request),
        request=dict(request),
        status=status,
        media_type=media_type,
        chunks=tuple(chunks),
    )


MissKind = Literal[
    "unrecorded_request",
    "repeated_request",
    "malformed_request",
    "unrecorded_clip",
]


def judge_latest(request: Mapping[str, Any]) -> str | None:
    contents = request.get("body", {}).get("contents") or []
    if not contents or not isinstance(contents[-1], dict):
        return None
    text = "".join(
        str(part.get("text", ""))
        for part in contents[-1].get("parts") or []
        if isinstance(part, dict)
    )
    try:
        window = json.loads(text)
    except ValueError:
        return None
    latest = window.get("latest") if isinstance(window, dict) else None
    if not isinstance(latest, dict) or not isinstance(latest.get("text"), str):
        return None
    return str(latest["text"]).strip()


@dataclass(slots=True)
class HeardTranscripts:
    interim: set[str] = field(default_factory=set)
    final: set[str] = field(default_factory=set)

    def note(self, message: Mapping[str, Any]) -> None:
        if message.get("type") != "transcript":
            return
        text = str(message.get("transcript", "")).strip()
        if text:
            (self.final if message.get("final") else self.interim).add(text)

    def speculative(self, request: Mapping[str, Any]) -> bool:
        latest = judge_latest(request)
        return latest is not None and latest in self.interim and latest not in self.final


@dataclass(frozen=True, slots=True)
class ReplayMiss:
    kind: MissKind
    route: str
    request_sha256: str | None
    detail: str
    nearest: str | None = None
    request: Mapping[str, Any] | None = None

    def summary(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "route": self.route,
            "request_sha256": self.request_sha256,
            "detail": self.detail,
            "judge_latest": judge_latest(self.request) if self.request is not None else None,
            "nearest_recorded_difference": self.nearest,
        }


def _nearest(asked: Mapping[str, Any], recorded: Sequence[ModelExchange]) -> str | None:
    same_route = [exchange for exchange in recorded if exchange.route == asked["route"]]
    if not same_route:
        return None
    text = json.dumps(asked["body"], sort_keys=True, ensure_ascii=False, indent=0).splitlines()
    best = max(
        same_route,
        key=lambda exchange: difflib.SequenceMatcher(
            None,
            text,
            json.dumps(
                exchange.request["body"], sort_keys=True, ensure_ascii=False, indent=0
            ).splitlines(),
            autojunk=False,
        ).ratio(),
    )
    other = json.dumps(
        best.request["body"], sort_keys=True, ensure_ascii=False, indent=0
    ).splitlines()
    matcher = difflib.SequenceMatcher(None, other, text, autojunk=False)
    changes: list[str] = []
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        for before, after in zip(
            other[old_start:old_end] or [""], text[new_start:new_end] or [""], strict=False
        ):
            changes.append(_difference(before, after))
    return "\n".join(changes[:12])


def _difference(before: str, after: str) -> str:
    start = next(
        (
            index
            for index, pair in enumerate(zip(before, after, strict=False))
            if pair[0] != pair[1]
        ),
        min(len(before), len(after)),
    )
    left = max(0, start - DIFF_CONTEXT)
    return (
        f"at {start}: recorded …{before[left : start + DIFF_CONTEXT]}… "
        f"asked …{after[left : start + DIFF_CONTEXT]}…"
    )


@dataclass(slots=True)
class ReplayLedger:
    exchanges: tuple[ModelExchange, ...]
    takes: Mapping[str, tuple[tuple[SpeechMessage, ...], ...]]
    rules: Sequence[VolatileRule] = VOLATILE_RULES
    misses: list[ReplayMiss] = field(default_factory=list)
    served: Counter[str] = field(default_factory=Counter)
    heard: set[tuple[str, int]] = field(default_factory=set)
    _by_digest: dict[str, list[ModelExchange]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        grouped: defaultdict[str, list[ModelExchange]] = defaultdict(list)
        for exchange in self.exchanges:
            grouped[matching_digest(exchange.request, self.rules)].append(exchange)
        self._by_digest = dict(grouped)

    @classmethod
    def load(cls, path: Path, rules: Sequence[VolatileRule] = VOLATILE_RULES) -> "ReplayLedger":
        lines = read_cassette(path)
        exchanges = tuple(line for line in lines if isinstance(line, ModelExchange))
        speech: defaultdict[str, defaultdict[int, list[SpeechMessage]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for line in lines:
            if isinstance(line, SpeechMessage):
                speech[line.clip][line.take].append(line)
        takes = {
            clip: tuple(tuple(by_take[take]) for take in sorted(by_take))
            for clip, by_take in speech.items()
        }
        for clip, recorded in takes.items():
            numbers = sorted({take[0].take for take in recorded})
            if numbers != list(range(1, len(numbers) + 1)):
                raise CassetteError(f"takes of {clip} are not numbered 1..n: {numbers}")
        return cls(exchanges, takes, rules)

    def respond(self, route: str, body: bytes) -> ModelExchange | ReplayMiss:
        try:
            request = canonical_request(route, body, self.rules)
        except CassetteError as error:
            malformed = ReplayMiss("malformed_request", route, None, str(error))
            self.misses.append(malformed)
            return malformed
        digest = request_digest(request)
        recorded = self._by_digest.get(digest, [])
        position = self.served[digest]
        if position < len(recorded):
            self.served[digest] += 1
            return recorded[position]
        miss = ReplayMiss(
            kind="repeated_request" if recorded else "unrecorded_request",
            route=route,
            request_sha256=digest,
            detail=(
                f"asked {position + 1} times, recorded {len(recorded)}"
                if recorded
                else "no recorded exchange has this canonical request"
            ),
            nearest=None if recorded else _nearest(request, self.exchanges),
            request=request,
        )
        self.misses.append(miss)
        return miss

    def speech(self, clip: str, take: int) -> tuple[SpeechMessage, ...] | ReplayMiss:
        recorded = self.takes.get(clip, ())
        if 1 <= take <= len(recorded):
            self.heard.add((clip, take))
            return recorded[take - 1]
        miss = ReplayMiss(
            kind="unrecorded_clip",
            route="stt",
            request_sha256=None,
            detail=f"{clip} was spoken as take {take}, recorded {len(recorded)} takes",
        )
        self.misses.append(miss)
        return miss

    def used_exchanges(self) -> list[ModelExchange]:
        return [
            exchange
            for digest, exchanges in self._by_digest.items()
            for exchange in exchanges[: self.served[digest]]
        ]

    def unused(
        self, heard: HeardTranscripts
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        fatal: list[dict[str, object]] = []
        speculative: list[dict[str, object]] = []
        for digest, exchanges in self._by_digest.items():
            if self.served[digest] >= len(exchanges):
                continue
            left: dict[str, object] = {
                "route": exchanges[0].route,
                "request_sha256": digest,
                "unused": len(exchanges) - self.served[digest],
                "judge_latest": judge_latest(exchanges[0].request),
            }
            (speculative if heard.speculative(exchanges[0].request) else fatal).append(left)
        fatal += [
            {"route": "stt", "clip": clip, "take": take}
            for clip, takes in self.takes.items()
            for take in range(1, len(takes) + 1)
            if (clip, take) not in self.heard
        ]
        return fatal, speculative

    def summary(self, heard: HeardTranscripts) -> dict[str, object]:
        fatal = [
            miss
            for miss in self.misses
            if miss.request is None or not heard.speculative(miss.request)
        ]
        speculative = [miss for miss in self.misses if miss not in fatal]
        unused, unused_speculative = self.unused(heard)
        return {
            "recorded_exchanges": len(self.exchanges),
            "served_exchanges": sum(self.served.values()),
            "recorded_takes": sum(len(takes) for takes in self.takes.values()),
            "heard_takes": len(self.heard),
            "misses": [miss.summary() for miss in fatal],
            "unused": unused,
            "speculative_misses": [miss.summary() for miss in speculative],
            "speculative_unused": unused_speculative,
            "closed": not fatal and not unused,
        }
