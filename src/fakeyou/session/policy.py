from dataclasses import dataclass
from typing import Literal

from fakeyou.session.intent import (
    Answer,
    ChatRequest,
    Clarify,
    Compound,
    Intent,
    MeetAction,
    NoResponse,
    Output,
)

Permission = Literal[
    "explain_facts",
    "discuss_options",
    "ask_brief_questions",
    "meet_act",
    "meet_chat",
    "meet_leave",
]
Channel = Literal["speech", "chat"]
ActorKind = Literal["participant", "unattributed"]
RefusalReason = Literal[
    "unattributed_actor", "not_permitted", "browser_unavailable", "chat_not_permitted"
]


@dataclass(frozen=True, slots=True)
class Actor:
    kind: ActorKind
    device_id: str | None = None


UNATTRIBUTED = Actor("unattributed")


@dataclass(frozen=True, slots=True)
class Refusal:
    what: str
    reason: RefusalReason


@dataclass(frozen=True, slots=True)
class Authorization:
    intent: Intent
    refusals: tuple[Refusal, ...] = ()


@dataclass(frozen=True, slots=True)
class Grant:
    permissions: frozenset[Permission]
    browser_available: bool = True


def _action_refusal(action: MeetAction, actor: Actor, grant: Grant) -> Refusal | None:
    if not grant.browser_available:
        return Refusal(action.kind, "browser_unavailable")
    if actor.kind != "participant":
        return Refusal(action.kind, "unattributed_actor")
    needed: Permission = "meet_leave" if action.kind == "leave" else "meet_act"
    if needed not in grant.permissions:
        return Refusal(action.kind, "not_permitted")
    return None


def _chat_refusal(actor: Actor, grant: Grant) -> Refusal | None:
    if not grant.browser_available:
        return Refusal("send_chat", "browser_unavailable")
    if actor.kind != "participant":
        return Refusal("send_chat", "unattributed_actor")
    if "meet_chat" not in grant.permissions:
        return Refusal("send_chat", "chat_not_permitted")
    return None


def _output(
    output: Output | Clarify, channel: Channel, actor: Actor, grant: Grant
) -> tuple[Intent, tuple[Refusal, ...]]:
    writes_chat = isinstance(output, ChatRequest) or channel == "chat"
    if not writes_chat:
        return output, ()
    refusal = _chat_refusal(actor, grant)
    if refusal is None:
        return output, ()
    if channel == "chat":
        return NoResponse(refusal.reason), (refusal,)
    if isinstance(output, ChatRequest):
        return Answer(output.evidence), (refusal,)
    return output, (refusal,)


def authorize(intent: Intent, *, actor: Actor, channel: Channel, grant: Grant) -> Authorization:
    match intent:
        case NoResponse():
            return Authorization(intent)
        case Answer() | ChatRequest() | Clarify():
            allowed, refusals = _output(intent, channel, actor, grant)
            return Authorization(allowed, refusals)
        case MeetAction():
            refusal = _action_refusal(intent, actor, grant)
            if refusal is None:
                return Authorization(intent)
            allowed, refusals = _output(Answer(), channel, actor, grant)
            return Authorization(allowed, (refusal, *refusals))
        case Compound(action=action, output=output):
            refusal = _action_refusal(action, actor, grant)
            allowed, refusals = _output(output, channel, actor, grant)
            if refusal is not None:
                return Authorization(allowed, (refusal, *refusals))
            if isinstance(allowed, Answer | ChatRequest):
                return Authorization(Compound(action, allowed), refusals)
            return Authorization(action, refusals)
