from pathlib import Path
from urllib.parse import urlparse

from fakeyou.catalog.errors import ErrorCode, FakeYouError


def validate_meeting_request(
    *,
    link: str,
    lobby_timeout: float | None,
    duration: float | None,
    initial_speech: str | None,
    audio: bool,
    conversation: bool,
    memory: bool,
    persona_path: Path | None,
    brief_path: Path | None,
    meeting_id: str | None,
    wake_phrase: str | None,
    barge_in: bool,
) -> None:
    parsed = urlparse(link)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "meet.google.com"
        or parsed.path.strip("/") == ""
    ):
        raise FakeYouError(ErrorCode.MEETING_LINK_INVALID)
    if lobby_timeout is not None and not lobby_timeout > 0:
        raise FakeYouError(ErrorCode.MEETING_LOBBY_TIMEOUT_NOT_POSITIVE)
    if duration is not None and not duration > 0:
        raise FakeYouError(ErrorCode.MEETING_DURATION_NOT_POSITIVE)
    if initial_speech is not None and (not audio or not initial_speech.strip()):
        raise FakeYouError(ErrorCode.MEETING_SPEAK_REQUIRES_AUDIO)
    if conversation and not audio:
        raise FakeYouError(ErrorCode.MEETING_CONVERSE_REQUIRES_AUDIO)
    if memory and not conversation:
        raise FakeYouError(ErrorCode.MEETING_MEMORY_REQUIRES_CONVERSE)
    if persona_path is not None and not conversation:
        raise FakeYouError(ErrorCode.MEETING_PERSONA_REQUIRES_CONVERSE)
    if memory and persona_path is None:
        raise FakeYouError(ErrorCode.MEETING_MEMORY_REQUIRES_PERSONA)
    if brief_path is not None and not conversation:
        raise FakeYouError(ErrorCode.MEETING_BRIEF_REQUIRES_CONVERSE)
    if brief_path is not None and persona_path is None:
        raise FakeYouError(ErrorCode.MEETING_BRIEF_REQUIRES_PERSONA)
    if brief_path is not None and not meeting_id:
        raise FakeYouError(ErrorCode.MEETING_BRIEF_REQUIRES_MEETING_ID)
    if wake_phrase and not conversation:
        raise FakeYouError(ErrorCode.MEETING_WAKE_PHRASE_REQUIRES_CONVERSE)
    if barge_in and not conversation:
        raise FakeYouError(ErrorCode.MEETING_BARGE_IN_REQUIRES_CONVERSE)
