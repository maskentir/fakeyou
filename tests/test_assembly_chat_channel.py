from pathlib import Path

from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.orchestration.assembly import (
    DEFAULT_AGENT_WIRING,
    AgentWiring,
    TurnBehavior,
    _ConversationBuilder,
)
from fakeyou.orchestration.meet_actions import MeetActionDispatcher, MeetChatChannel
from fakeyou.persistence.brief import BriefPermission, MeetingBrief


def _brief(*permissions: BriefPermission) -> MeetingBrief:
    return MeetingBrief(
        schema_version="meeting_brief/v2",
        owner_id=SessionId("b-owner"),
        twin_id=SessionId("b-twin"),
        meeting_id=SessionId("b-meeting"),
        permissions=frozenset(permissions),
        knowledge_scope="persona_memory",
    )


def _dispatcher(tmp_path: Path) -> MeetActionDispatcher:
    return MeetActionDispatcher(
        tmp_path / "record.jsonl",
        meeting_id="b-meeting",
        enabled=True,
        chat_per_minute=3,
    )


def test_the_chat_channel_is_wired_when_the_brief_allows_chat(
    settings: Settings, tmp_path: Path
) -> None:
    dispatcher = _dispatcher(tmp_path)
    builder = _ConversationBuilder(
        settings,
        AgentWiring(brief=_brief("meet_chat")),
        TurnBehavior(on_transition=lambda *_args: None, meet_dispatcher=dispatcher),
        Diagnostics(),
    )

    assert isinstance(builder.chat_channel(), MeetChatChannel)


def test_without_the_chat_permission_there_is_no_chat_channel(
    settings: Settings, tmp_path: Path
) -> None:
    dispatcher = _dispatcher(tmp_path)
    builder = _ConversationBuilder(
        settings,
        AgentWiring(brief=_brief("explain_facts")),
        TurnBehavior(on_transition=lambda *_args: None, meet_dispatcher=dispatcher),
        Diagnostics(),
    )

    assert builder.chat_channel() is None


def test_without_a_brief_there_is_no_chat_channel(settings: Settings, tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    builder = _ConversationBuilder(
        settings,
        DEFAULT_AGENT_WIRING,
        TurnBehavior(on_transition=lambda *_args: None, meet_dispatcher=dispatcher),
        Diagnostics(),
    )

    assert builder.chat_channel() is None
