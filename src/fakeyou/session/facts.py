from dataclasses import dataclass
from typing import Literal

from fakeyou.session.commands import CommandKind, CommandOutcome, CommandState
from fakeyou.session.identity import presence
from fakeyou.session.world import SessionState

StateQuestion = Literal[
    "attendance", "participant_count", "presenter", "screen_title", "own_action_status"
]
Namespace = Literal["meeting_live", "conversation", "brief", "persona", "memory", "own_action"]
Modality = Literal[
    "directly_observed",
    "protocol_reported",
    "participant_claimed",
    "screen_read",
    "owner_approved",
    "memory_recalled",
    "inferred",
]
Disclosure = Literal["room_speakable", "owner_only", "internal_instruction"]
RevisionDomain = Literal["presence", "screen", "health"]
UnknownReason = Literal["stale_source", "not_in_call", "no_record"]


@dataclass(frozen=True, slots=True)
class Attendance:
    names: tuple[str, ...]
    unnamed: int
    complete: bool


@dataclass(frozen=True, slots=True)
class ParticipantCount:
    minimum: int
    maximum: int | None

    @property
    def exact(self) -> bool:
        return self.maximum == self.minimum


@dataclass(frozen=True, slots=True)
class Presenter:
    name: str | None
    presenting: bool


@dataclass(frozen=True, slots=True)
class ScreenTitle:
    title: str | None
    sharing: bool


@dataclass(frozen=True, slots=True)
class OwnAction:
    kind: CommandKind
    state: CommandState


@dataclass(frozen=True, slots=True)
class Unknown:
    question: StateQuestion
    reason: UnknownReason


Proposition = Attendance | ParticipantCount | Presenter | ScreenTitle | OwnAction | Unknown


@dataclass(frozen=True, slots=True)
class Fact:
    fact_id: str
    namespace: Namespace
    modality: Modality
    proposition: Proposition
    valid_from_seq: int
    domain: RevisionDomain
    revision: int
    disclosure: Disclosure = "room_speakable"


def _revision(state: SessionState, domain: RevisionDomain) -> int:
    revisions = state.revisions
    return {
        "presence": revisions.presence,
        "screen": revisions.screen,
        "health": revisions.health,
    }[domain]


LIVE: tuple[Namespace, Modality] = ("meeting_live", "protocol_reported")


def _fact(
    state: SessionState,
    proposition: Proposition,
    *,
    domain: RevisionDomain,
    origin: tuple[Namespace, Modality] = LIVE,
) -> Fact:
    label = proposition.question if isinstance(proposition, Unknown) else type(proposition).__name__
    namespace, modality = origin
    return Fact(
        fact_id=f"{label}@{state.seq}",
        namespace=namespace,
        modality=modality,
        proposition=proposition,
        valid_from_seq=state.seq,
        domain=domain,
        revision=_revision(state, domain),
    )


def _attendance(state: SessionState) -> Fact:
    fact = presence(state)
    if fact.status in ("stale", "not_in_call"):
        reason: UnknownReason = "stale_source" if fact.status == "stale" else "not_in_call"
        return _fact(state, Unknown("attendance", reason), domain="presence")
    names = tuple(
        dict.fromkeys(
            participant.display_claim
            for participant in fact.participants
            if participant.display_claim
        )
    )
    unnamed = sum(1 for participant in fact.participants if not participant.display_claim)
    return _fact(
        state,
        Attendance(names, unnamed + fact.unresolved, fact.complete),
        domain="presence",
        origin=("meeting_live", "participant_claimed"),
    )


def _count(state: SessionState) -> Fact:
    fact = presence(state)
    if fact.status in ("stale", "not_in_call"):
        reason: UnknownReason = "stale_source" if fact.status == "stale" else "not_in_call"
        return _fact(state, Unknown("participant_count", reason), domain="presence")
    return _fact(
        state,
        ParticipantCount(fact.confirmed_min, fact.plausible_max),
        domain="presence",
    )


def _presenter(state: SessionState) -> Fact:
    if state.health.sources.get("meet_protocol") in ("stale", "down"):
        return _fact(state, Unknown("presenter", "stale_source"), domain="presence")
    screen = state.world.screen
    device = screen.presenter_device_id if screen is not None else None
    endpoint = state.world.endpoints.get(device) if device is not None else None
    if device is None:
        return _fact(state, Presenter(None, presenting=False), domain="screen")
    name = endpoint.display_name if endpoint is not None and endpoint.display_name else None
    return _fact(
        state,
        Presenter(name, presenting=True),
        domain="screen",
        origin=("meeting_live", "participant_claimed"),
    )


def _screen_title(state: SessionState) -> Fact:
    screen = state.world.screen
    if screen is None or screen.presenter_device_id is None:
        return _fact(state, ScreenTitle(None, sharing=False), domain="screen")
    return _fact(
        state,
        ScreenTitle(screen.title, sharing=True),
        domain="screen",
        origin=("meeting_live", "screen_read"),
    )


def _own_action(state: SessionState, actions: tuple[CommandOutcome, ...]) -> Fact:
    if not actions:
        return _fact(
            state,
            Unknown("own_action_status", "no_record"),
            domain="health",
            origin=("own_action", "directly_observed"),
        )
    latest = actions[-1]
    return _fact(
        state,
        OwnAction(latest.kind, latest.state),
        domain="health",
        origin=("own_action", "directly_observed"),
    )


def state_fact(
    question: StateQuestion, state: SessionState, actions: tuple[CommandOutcome, ...] = ()
) -> Fact:
    match question:
        case "attendance":
            return _attendance(state)
        case "participant_count":
            return _count(state)
        case "presenter":
            return _presenter(state)
        case "screen_title":
            return _screen_title(state)
        case "own_action_status":
            return _own_action(state, actions)


def still_valid(fact: Fact, state: SessionState) -> bool:
    return _revision(state, fact.domain) == fact.revision
