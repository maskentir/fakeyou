import json
from typing import Any, Literal

from fakeyou.foundation.resources import read_package_text
from fakeyou.session.commands import TERMINAL_COMMAND_STATES, CommandState
from fakeyou.session.facts import (
    Attendance,
    OwnAction,
    ParticipantCount,
    Presenter,
    Proposition,
    ScreenTitle,
    Unknown,
)

_PACKS: dict[str, dict[str, Any]] = json.loads(read_package_text("state_speech.json"))
_NOT_DONE: frozenset[CommandState] = frozenset(
    {"refused", "cancelled_before_boundary", "failed_before_boundary", "failed_after_boundary"}
)


def locale_of(language: str) -> str | None:
    base = language.split("-", 1)[0].casefold()
    return base if base in _PACKS else None


def _joined(pack: dict[str, Any], names: tuple[str, ...]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return str(pack["join"]["pair"]).format(first=names[0], last=names[1])
    return str(pack["join"]["list"]).format(rest=", ".join(names[:-1]), last=names[-1])


def _attendance(pack: dict[str, Any], fact: Attendance) -> str:
    lines = pack["attendance"]
    if not fact.names:
        return str(lines["unnamed_only"] if fact.unnamed else lines["nobody"])
    names = _joined(pack, fact.names)
    if fact.complete:
        return str(lines["complete"]).format(names=names)
    if fact.unnamed:
        return str(lines["partial_unnamed"]).format(names=names, unnamed=fact.unnamed)
    return str(lines["partial"]).format(names=names)


def _count(pack: dict[str, Any], fact: ParticipantCount) -> str:
    lines = pack["participant_count"]
    if not fact.exact:
        if fact.minimum == 0:
            return str(pack["unknown"]["participant_count"])
        if fact.minimum == 1:
            return str(lines["at_least_one"])
        return str(lines["at_least"]).format(count=fact.minimum)
    if fact.minimum == 0:
        return str(lines["none"])
    if fact.minimum == 1:
        return str(lines["exact_one"])
    return str(lines["exact"]).format(count=fact.minimum)


def _presenter(pack: dict[str, Any], fact: Presenter) -> str:
    lines = pack["presenter"]
    if not fact.presenting:
        return str(lines["nobody"])
    if fact.name is None:
        return str(lines["unnamed"])
    return str(lines["named"]).format(name=fact.name)


def _screen(pack: dict[str, Any], fact: ScreenTitle) -> str:
    lines = pack["screen_title"]
    if not fact.sharing:
        return str(lines["nobody"])
    if not fact.title:
        return str(lines["untitled"])
    return str(lines["titled"]).format(title=fact.title)


def _own_action(pack: dict[str, Any], fact: OwnAction) -> str:
    noun, past = pack["actions"][fact.kind]
    lines = pack["own_action_status"]
    if fact.state in _NOT_DONE:
        line = lines["not_done"]
    elif fact.state not in TERMINAL_COMMAND_STATES:
        line = lines["pending"]
    else:
        line = lines[fact.state]
    rendered = str(line).format(action=noun, action_past=past)
    return rendered[0].upper() + rendered[1:]


def render(proposition: Proposition, locale: str) -> str:
    pack = _PACKS[locale]
    match proposition:
        case Attendance():
            return _attendance(pack, proposition)
        case ParticipantCount():
            return _count(pack, proposition)
        case Presenter():
            return _presenter(pack, proposition)
        case ScreenTitle():
            return _screen(pack, proposition)
        case OwnAction():
            return _own_action(pack, proposition)
        case Unknown():
            return str(pack["unknown"][proposition.question])


SpeechLine = Literal["owner_commitment", "unsure", "evidence_unavailable"]


def speech_line(line: SpeechLine, locale: str) -> str:
    return str(_PACKS[locale]["speech"][line])
