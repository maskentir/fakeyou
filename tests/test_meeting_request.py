from pathlib import Path
from typing import Any

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.meeting_request import MeetingRequest, validate_meeting_request
from tests._error_support import message_pattern

LINK = "https://meet.google.com/abc-defg-hij"
PERSONA = Path("persona.json")
BRIEF = Path("brief.json")
VALID_REQUEST: dict[str, Any] = {
    "link": LINK,
    "lobby_timeout": None,
    "duration": None,
    "initial_speech": None,
    "audio": True,
    "conversation": True,
    "memory": False,
    "persona_path": PERSONA,
    "brief_path": None,
    "meeting_id": "meeting-1",
    "wake_phrase": None,
    "barge_in": False,
}


def request(**overrides: Any) -> dict[str, Any]:
    return {**VALID_REQUEST, **overrides}


@pytest.mark.parametrize("link", [f"{LINK}?authuser=0", f"{LINK}?hl=fr"])
def test_meet_links_with_allowed_query_parameters_are_accepted(link: str) -> None:
    validate_meeting_request(MeetingRequest(**request(link=link)))


def test_a_request_that_honours_every_rule_raises_nothing() -> None:
    validate_meeting_request(
        MeetingRequest(
            **request(
                lobby_timeout=60.0,
                duration=3600.0,
                initial_speech="Hello.",
                memory=True,
                brief_path=BRIEF,
                wake_phrase="hey fakeyou",
                barge_in=True,
            )
        )
    )


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        pytest.param({"link": ""}, ErrorCode.MEETING_LINK_INVALID, id="link-empty"),
        pytest.param({"link": "not-a-url"}, ErrorCode.MEETING_LINK_INVALID, id="link-no-scheme"),
        pytest.param(
            {"link": "http://meet.google.com/abc-defg-hij"},
            ErrorCode.MEETING_LINK_INVALID,
            id="link-http",
        ),
        pytest.param(
            {"link": "https://example.com/abc-defg-hij"},
            ErrorCode.MEETING_LINK_INVALID,
            id="link-other-host",
        ),
        pytest.param(
            {"link": "https://meet.google.com/"}, ErrorCode.MEETING_LINK_INVALID, id="link-no-code"
        ),
        pytest.param(
            {"link": "https://meet.google.com:8443/abc-defg-hij"},
            ErrorCode.MEETING_LINK_INVALID,
            id="link-port",
        ),
        pytest.param(
            {"link": "https://user:secret@meet.google.com/abc-defg-hij"},
            ErrorCode.MEETING_LINK_INVALID,
            id="link-userinfo",
        ),
        pytest.param(
            {"link": "https://meet.google.com/abc-defg-hij/extra"},
            ErrorCode.MEETING_LINK_INVALID,
            id="link-extra-path",
        ),
        pytest.param(
            {"link": "https://meet.google.com/abcd-efgh-ijk"},
            ErrorCode.MEETING_LINK_INVALID,
            id="link-wrong-code-shape",
        ),
        pytest.param(
            {"link": "https://meet.google.com/abc-defg-hij?redirect=evil"},
            ErrorCode.MEETING_LINK_INVALID,
            id="link-unknown-query",
        ),
        pytest.param(
            {"link": "https://meet.google.com/abc-defg-hij#frag"},
            ErrorCode.MEETING_LINK_INVALID,
            id="link-fragment",
        ),
        pytest.param(
            {"lobby_timeout": 0.0},
            ErrorCode.MEETING_LOBBY_TIMEOUT_NOT_POSITIVE,
            id="lobby-timeout-zero",
        ),
        pytest.param(
            {"lobby_timeout": -1.0},
            ErrorCode.MEETING_LOBBY_TIMEOUT_NOT_POSITIVE,
            id="lobby-timeout-negative",
        ),
        pytest.param(
            {"lobby_timeout": float("nan")},
            ErrorCode.MEETING_LOBBY_TIMEOUT_NOT_POSITIVE,
            id="lobby-timeout-nan",
        ),
        pytest.param(
            {"duration": 0.0}, ErrorCode.MEETING_DURATION_NOT_POSITIVE, id="duration-zero"
        ),
        pytest.param(
            {"duration": -5.0}, ErrorCode.MEETING_DURATION_NOT_POSITIVE, id="duration-negative"
        ),
        pytest.param(
            {"initial_speech": "Hello.", "audio": False, "conversation": False},
            ErrorCode.MEETING_SPEAK_REQUIRES_AUDIO,
            id="speak-without-audio",
        ),
        pytest.param(
            {"initial_speech": "   "},
            ErrorCode.MEETING_SPEAK_REQUIRES_AUDIO,
            id="speak-blank-text",
        ),
        pytest.param(
            {"audio": False},
            ErrorCode.MEETING_CONVERSE_REQUIRES_AUDIO,
            id="converse-without-audio",
        ),
        pytest.param(
            {"memory": True, "conversation": False, "persona_path": None},
            ErrorCode.MEETING_MEMORY_REQUIRES_CONVERSE,
            id="memory-without-converse",
        ),
        pytest.param(
            {"conversation": False},
            ErrorCode.MEETING_PERSONA_REQUIRES_CONVERSE,
            id="persona-without-converse",
        ),
        pytest.param(
            {"memory": True, "persona_path": None},
            ErrorCode.MEETING_MEMORY_REQUIRES_PERSONA,
            id="memory-without-persona",
        ),
        pytest.param(
            {"brief_path": BRIEF, "conversation": False, "persona_path": None},
            ErrorCode.MEETING_BRIEF_REQUIRES_CONVERSE,
            id="brief-without-converse",
        ),
        pytest.param(
            {"brief_path": BRIEF, "persona_path": None},
            ErrorCode.MEETING_BRIEF_REQUIRES_PERSONA,
            id="brief-without-persona",
        ),
        pytest.param(
            {"brief_path": BRIEF, "meeting_id": None},
            ErrorCode.MEETING_BRIEF_REQUIRES_MEETING_ID,
            id="brief-without-meeting-id",
        ),
        pytest.param(
            {"brief_path": BRIEF, "meeting_id": ""},
            ErrorCode.SESSION_ID_INVALID,
            id="brief-empty-meeting-id",
        ),
        pytest.param(
            {"wake_phrase": "hey fakeyou", "conversation": False, "persona_path": None},
            ErrorCode.MEETING_WAKE_PHRASE_REQUIRES_CONVERSE,
            id="wake-phrase-without-converse",
        ),
        pytest.param(
            {"barge_in": True, "conversation": False, "persona_path": None},
            ErrorCode.MEETING_BARGE_IN_REQUIRES_CONVERSE,
            id="barge-in-without-converse",
        ),
    ],
)
def test_each_rule_raises_its_catalog_code(overrides: dict[str, Any], code: ErrorCode) -> None:
    with pytest.raises(FakeYouError, match=message_pattern(code)) as caught:
        validate_meeting_request(MeetingRequest(**request(**overrides)))
    assert caught.value.code is code


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        pytest.param({"meeting_id": "../x"}, "--meeting-id", id="meeting-traversal"),
        pytest.param({"meeting_id": "a/b"}, "--meeting-id", id="meeting-slash"),
        pytest.param({"meeting_id": ""}, "--meeting-id", id="meeting-empty"),
        pytest.param({"owner_id": "a/b"}, "--owner-id", id="owner-slash"),
        pytest.param({"owner_id": "../x"}, "--owner-id", id="owner-traversal"),
        pytest.param({"twin_id": "a/b"}, "--twin-id", id="twin-slash"),
        pytest.param({"twin_id": "../x"}, "--twin-id", id="twin-traversal"),
    ],
)
def test_meeting_owner_and_twin_ids_are_validated(overrides: dict[str, Any], field: str) -> None:
    with pytest.raises(FakeYouError, match=field) as caught:
        validate_meeting_request(MeetingRequest(**request(**overrides)))
    assert caught.value.code is ErrorCode.SESSION_ID_INVALID


def test_valid_owner_and_twin_ids_pass_the_request_check() -> None:
    validate_meeting_request(MeetingRequest(**request(owner_id="owner-9", twin_id="twin_9")))
