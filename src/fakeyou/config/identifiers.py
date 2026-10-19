import re
from typing import Annotated, NewType

from pydantic import StringConstraints

from fakeyou.catalog.errors import ErrorCode, FakeYouError

IDENTIFIER_BODY = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
_IDENTIFIER = re.compile(IDENTIFIER_BODY)

SessionId = NewType("SessionId", str)
SessionIdField = Annotated[SessionId, StringConstraints(pattern=rf"^{IDENTIFIER_BODY}$")]
IdentifierField = Annotated[str, StringConstraints(pattern=rf"^{IDENTIFIER_BODY}$")]


def is_identifier(value: str) -> bool:
    return _IDENTIFIER.fullmatch(value) is not None


def parse_session_id(raw: str, *, field: str) -> SessionId:
    if not is_identifier(raw):
        raise FakeYouError(ErrorCode.SESSION_ID_INVALID, field=field, value=raw)
    return SessionId(raw)


def verify_chat_thread_id(thread: str) -> None:
    if not is_identifier(thread):
        raise FakeYouError(ErrorCode.CHAT_THREAD_INVALID)


def verify_memory_group_id(group_id: str) -> None:
    if not is_identifier(group_id):
        raise FakeYouError(ErrorCode.NOTE_GROUP_ID_INVALID)
