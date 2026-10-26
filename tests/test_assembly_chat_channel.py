import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast, override

from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.conversation import MeetingAgent, MeetingResponder
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.meet.meet_action_ledger import MeetActionLedger
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.orchestration.assembly import (
    DEFAULT_AGENT_WIRING,
    AgentWiring,
    TurnBehavior,
    _ConversationBuilder,
)
from fakeyou.orchestration.meet_actions import MeetActionDispatcher, MeetChatChannel
from fakeyou.persistence.brief import BriefPermission, KnowledgeScope, MeetingBrief
from tests._audio_fakes import FakeReplyAudio
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import IN_CALL, meet_page
from tests._meet_support import CHAT_INPUT_XPATH, CHAT_TEXT, ECHO_CONTENT, with_enter
from tests._persistence_fakes import RecordingJournal
from tests._persona_support import profile


def _brief(
    *permissions: BriefPermission, knowledge_scope: KnowledgeScope = "persona_memory"
) -> MeetingBrief:
    return MeetingBrief(
        schema_version="meeting_brief/v2",
        owner_id=SessionId("b-owner"),
        twin_id=SessionId("b-twin"),
        meeting_id=SessionId("b-meeting"),
        permissions=frozenset(permissions),
        knowledge_scope=knowledge_scope,
    )


def _dispatcher(tmp_path: Path) -> MeetActionDispatcher:
    return MeetActionDispatcher(
        tmp_path / "record.jsonl",
        meeting_id="b-meeting",
        enabled=True,
        chat_per_minute=3,
    )


def _builder(settings: Settings, tmp_path: Path, wiring: AgentWiring) -> _ConversationBuilder:
    builder = _ConversationBuilder(
        settings,
        wiring,
        TurnBehavior(on_transition=lambda *_args: None, meet_dispatcher=_dispatcher(tmp_path)),
        Diagnostics(),
    )
    builder.build_persona()
    return builder


def test_the_chat_channel_is_wired_when_the_brief_allows_chat(
    settings: Settings, tmp_path: Path
) -> None:
    builder = _builder(
        settings,
        tmp_path,
        AgentWiring(
            profile=profile(allowed_tools=["meet_send_chat"]),
            brief=_brief("meet_chat"),
        ),
    )

    assert isinstance(builder.chat_channel(), MeetChatChannel)


def test_without_the_chat_permission_there_is_no_chat_channel(
    settings: Settings, tmp_path: Path
) -> None:
    builder = _builder(
        settings,
        tmp_path,
        AgentWiring(
            profile=profile(allowed_tools=["meet_send_chat"]),
            brief=_brief("explain_facts"),
        ),
    )

    assert builder.chat_channel() is None


def test_without_a_brief_there_is_no_chat_channel(settings: Settings, tmp_path: Path) -> None:
    builder = _builder(settings, tmp_path, DEFAULT_AGENT_WIRING)

    assert builder.chat_channel() is None


def test_a_brief_only_brief_gets_no_chat_channel(settings: Settings, tmp_path: Path) -> None:
    builder = _builder(
        settings,
        tmp_path,
        AgentWiring(
            profile=profile(allowed_tools=["meet_send_chat"]),
            brief=_brief("meet_chat", knowledge_scope="brief_only"),
        ),
    )

    assert builder.chat_channel() is None


def test_a_persona_without_the_chat_tool_gets_no_chat_channel(
    settings: Settings, tmp_path: Path
) -> None:
    builder = _builder(
        settings,
        tmp_path,
        AgentWiring(
            profile=profile(allowed_tools=["recall_meeting_context"]),
            brief=_brief("meet_chat"),
        ),
    )

    assert builder.chat_channel() is None


class _FixedResponder(MeetingResponder):
    @override
    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> SpokenChunk:
        return SpokenChunk(text=CHAT_TEXT, language="en")


async def test_a_chat_line_reaches_the_page_through_the_assembled_conversation(
    settings: Settings, tmp_path: Path
) -> None:
    journal = RecordingJournal()
    record = tmp_path / "record.jsonl"
    dispatcher = MeetActionDispatcher(
        record, meeting_id="b-meeting", enabled=True, chat_per_minute=3, journal=journal
    )
    page = meet_page([IN_CALL, ECHO_CONTENT, ECHO_CONTENT], visible={CHAT_INPUT_XPATH: False})
    clock = FakeClock()
    with_enter(page)
    dispatcher.bind_page(page, sleep=clock.sleep, monotonic=clock.monotonic, stop=asyncio.Event())
    builder = _ConversationBuilder(
        settings,
        AgentWiring(
            profile=profile(allowed_tools=["meet_send_chat"]),
            brief=_brief("meet_chat"),
        ),
        TurnBehavior(
            on_transition=lambda *_args: None,
            twin_names=("Rani Twin",),
            meet_dispatcher=dispatcher,
        ),
        Diagnostics(journal=journal),
    )
    builder.build_persona()
    responder = _FixedResponder(cast(MeetingAgent, object()), NoHistory())
    turns = builder.build_turns(responder)
    audio = FakeReplyAudio()
    turns.lifecycle.bind(audio)

    accepted = turns.receive_chat(
        TranscriptEvent(
            "c1",
            datetime.now(UTC).isoformat(),
            "meeting_chat",
            "Rani Twin, can you share the deck?",
            finalized=True,
            participant_id="device-budi",
            speaker_evidence_id=None,
        )
    )
    for _ in range(50):
        await turns.lifecycle.wait_idle()
        if not turns.lifecycle.deferred.pending and not turns.lifecycle.is_busy:
            break
        await asyncio.sleep(0.02)
    await dispatcher.drain()
    dispatcher.close()
    await turns.close()

    assert accepted
    assert page.typed == [CHAT_TEXT]
    assert audio.spoken == []
    assert journal.events("chat_reply_finished")[0]["sent"] is True
    assert journal.events("chat_reply_dropped") == []
    intent = MeetActionLedger.load(record, "b-meeting").get("c1")
    assert intent is not None
    assert intent.state == "observed_done"
