import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import parse_session_id


@dataclass(frozen=True, slots=True)
class MeetingRequest:
    link: str
    lobby_timeout: float | None = None
    duration: float | None = None
    initial_speech: str | None = None
    audio: bool = False
    conversation: bool = False
    memory: bool = False
    persona_path: Path | None = None
    brief_path: Path | None = None
    meeting_id: str | None = None
    wake_phrase: str | None = None
    barge_in: bool = False
    owner_id: str | None = None
    twin_id: str | None = None


_MEET_CODE_PATH = re.compile(r"/[a-z]{3}-[a-z]{4}-[a-z]{3}")
_ALLOWED_LINK_QUERY: dict[str, re.Pattern[str]] = {
    "authuser": re.compile(r"\d+"),
    "hl": re.compile(r"[a-zA-Z-]+"),
}


def _check_link(link: str) -> None:
    parsed = urlparse(link)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "meet.google.com"
        or parsed.fragment
        or _MEET_CODE_PATH.fullmatch(parsed.path) is None
        or not _link_query_allowed(parsed.query)
    ):
        raise FakeYouError(ErrorCode.MEETING_LINK_INVALID)


def _link_query_allowed(query: str) -> bool:
    for key, value in parse_qsl(query, keep_blank_values=True):
        pattern = _ALLOWED_LINK_QUERY.get(key)
        if pattern is None or pattern.fullmatch(value) is None:
            return False
    return True


def _check_timeouts(lobby_timeout: float | None, duration: float | None) -> None:
    if lobby_timeout is not None and not lobby_timeout > 0:
        raise FakeYouError(ErrorCode.MEETING_LOBBY_TIMEOUT_NOT_POSITIVE)
    if duration is not None and not duration > 0:
        raise FakeYouError(ErrorCode.MEETING_DURATION_NOT_POSITIVE)


def _check_speech(initial_speech: str | None, *, audio: bool) -> None:
    if initial_speech is not None and (not audio or not initial_speech.strip()):
        raise FakeYouError(ErrorCode.MEETING_SPEAK_REQUIRES_AUDIO)


def _check_conversation_flags(
    *, audio: bool, conversation: bool, memory: bool, persona_path: Path | None
) -> None:
    if conversation and not audio:
        raise FakeYouError(ErrorCode.MEETING_CONVERSE_REQUIRES_AUDIO)
    if memory and not conversation:
        raise FakeYouError(ErrorCode.MEETING_MEMORY_REQUIRES_CONVERSE)
    if persona_path is not None and not conversation:
        raise FakeYouError(ErrorCode.MEETING_PERSONA_REQUIRES_CONVERSE)
    if memory and persona_path is None:
        raise FakeYouError(ErrorCode.MEETING_MEMORY_REQUIRES_PERSONA)


def _check_brief(
    brief_path: Path | None,
    *,
    conversation: bool,
    persona_path: Path | None,
    meeting_id: str | None,
) -> None:
    if brief_path is not None and not conversation:
        raise FakeYouError(ErrorCode.MEETING_BRIEF_REQUIRES_CONVERSE)
    if brief_path is not None and persona_path is None:
        raise FakeYouError(ErrorCode.MEETING_BRIEF_REQUIRES_PERSONA)
    if brief_path is not None and meeting_id is None:
        raise FakeYouError(ErrorCode.MEETING_BRIEF_REQUIRES_MEETING_ID)


def _check_addressing(wake_phrase: str | None, *, barge_in: bool, conversation: bool) -> None:
    if wake_phrase and not conversation:
        raise FakeYouError(ErrorCode.MEETING_WAKE_PHRASE_REQUIRES_CONVERSE)
    if barge_in and not conversation:
        raise FakeYouError(ErrorCode.MEETING_BARGE_IN_REQUIRES_CONVERSE)


def _check_identifiers(
    *, meeting_id: str | None, owner_id: str | None, twin_id: str | None
) -> None:
    for field, value in (
        ("--meeting-id", meeting_id),
        ("--owner-id", owner_id),
        ("--twin-id", twin_id),
    ):
        if value is not None:
            parse_session_id(value, field=field)


def validate_meeting_request(request: MeetingRequest) -> None:
    _check_link(request.link)
    _check_timeouts(request.lobby_timeout, request.duration)
    _check_speech(request.initial_speech, audio=request.audio)
    _check_conversation_flags(
        audio=request.audio,
        conversation=request.conversation,
        memory=request.memory,
        persona_path=request.persona_path,
    )
    _check_brief(
        request.brief_path,
        conversation=request.conversation,
        persona_path=request.persona_path,
        meeting_id=request.meeting_id,
    )
    _check_addressing(
        request.wake_phrase, barge_in=request.barge_in, conversation=request.conversation
    )
    _check_identifiers(
        meeting_id=request.meeting_id, owner_id=request.owner_id, twin_id=request.twin_id
    )
