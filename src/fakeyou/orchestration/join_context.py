import asyncio
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import ValidationError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.conversation.session import SessionConfig
from fakeyou.conversation.state import TERMINAL_PHASES
from fakeyou.db.engine import configured_url
from fakeyou.memory.persona import (
    PersonaProfile,
    load_operator_persona,
    meeting_tool_names,
    persona_digest,
)
from fakeyou.observability.provider_faults import trim_detail
from fakeyou.observability.report import describe_session
from fakeyou.orchestration.join_types import JoinPorts, JoinRequest
from fakeyou.orchestration.lifecycle import meeting_thread_id
from fakeyou.persistence.brief import (
    MeetingBrief,
    MeetingContext,
    brief_digest,
    context_path,
    load_meeting_brief,
    read_meeting_context,
    write_meeting_context,
)
from fakeyou.persistence.layout import MeetingDir, meeting_profile_dir


@dataclass(frozen=True, slots=True)
class PreparedJoin:
    request: JoinRequest
    config: SessionConfig
    record_path: Path
    profile: PersonaProfile | None
    brief: MeetingBrief | None
    meeting_context: MeetingContext | None


async def prepare_join(request: JoinRequest) -> PreparedJoin:
    config = build_session_config(request)
    profile, brief, meeting_context, config = await _load_join_context(request, config)
    config, record_path = prepare_session_paths(config, request.settings)
    return PreparedJoin(
        request=request,
        config=config,
        record_path=record_path,
        profile=profile,
        brief=brief,
        meeting_context=meeting_context,
    )


def build_session_config(request: JoinRequest) -> SessionConfig:
    settings = request.settings
    meeting_id = parse_session_id(
        request.meeting_id if request.meeting_id is not None else uuid.uuid4().hex[:12],
        field="--meeting-id",
    )
    owner_id = parse_session_id(
        request.identity.owner_id or settings.identity.owner_id, field="--owner-id"
    )
    twin_id = parse_session_id(
        request.identity.twin_id or settings.identity.twin_id, field="--twin-id"
    )
    resolved_headless = settings.browser.headless if request.headless is None else request.headless
    return SessionConfig(
        owner_id=owner_id,
        twin_id=twin_id,
        meeting_id=meeting_id,
        link=request.link,
        twin_name=request.twin_name or settings.identity.twin_name,
        browser_profile_dir=Path(request.profile_dir)
        if request.profile_dir
        else meeting_profile_dir(
            settings.paths.browser_profile_dir, twin_id=twin_id, meeting_id=meeting_id
        ),
        lobby_timeout_seconds=request.lobby_timeout
        if request.lobby_timeout is not None
        else settings.meeting.lobby_timeout_seconds,
        session_duration_seconds=request.duration
        if request.duration is not None
        else settings.meeting.session_duration_seconds,
        join_poll_interval_seconds=settings.meeting.join_poll_seconds,
        join_max_attempts=settings.meeting.join_max_attempts,
        join_retry_base_seconds=settings.meeting.join_retry_base_seconds,
        join_retry_cap_seconds=settings.meeting.join_retry_cap_seconds,
        monitor_interval_seconds=settings.meeting.monitor_poll_seconds,
        leave_timeout_seconds=settings.meeting.leave_timeout_seconds,
        warmup_browser=settings.browser.warmup,
        deaf_window_seconds=settings.audio_health.deaf_window_seconds,
        deaf_grace_seconds=settings.audio_health.deaf_grace_seconds,
        alone_leave_seconds=settings.meeting.alone_leave_seconds,
        attention_enabled=settings.perception.attention.enabled,
        attention_max_interjections=settings.perception.attention.max_interjections_per_10min,
        preflight_budget_seconds=settings.preflight.budget_seconds,
        headless=resolved_headless,
        license_key=settings.browser.license_key.get_secret_value()
        if settings.browser.license_key
        else None,
        database_url=configured_url(settings),
        database_connect_timeout_seconds=settings.database.connect_timeout_seconds,
        database_statement_timeout_seconds=settings.database.statement_timeout_seconds,
        database_keepalive_idle_seconds=settings.database.keepalive_idle_seconds,
        lifecycle_send_timeout_seconds=settings.database.send_timeout_seconds,
        drain_max_consecutive_failures=settings.audio.drain_max_consecutive_failures,
        drain_failure_journal_every=settings.audio.drain_failure_journal_every,
        participant_evidence_max_bytes=settings.conversation.participant_evidence_max_bytes,
        resume=request.resume,
    )


async def _load_join_context(
    request: JoinRequest, config: SessionConfig
) -> tuple[PersonaProfile | None, MeetingBrief | None, MeetingContext | None, SessionConfig]:
    profile: PersonaProfile | None = None
    brief: MeetingBrief | None = None
    meeting_context: MeetingContext | None = None
    if request.identity.persona_path is not None:
        profile = await load_operator_persona(request.identity, request.settings)
        if profile.language != request.settings.speech.language:
            raise FakeYouError(ErrorCode.MEETING_PERSONA_LANGUAGE_MISMATCH)
        if request.memory and not (profile.allowed_tools or profile.ingest_transcripts):
            raise FakeYouError(ErrorCode.MEETING_MEMORY_REQUIRES_RECALL_OR_INGEST)
        if request.brief_path is not None:
            brief = await _load_brief(request.brief_path, config)
            meeting_tool_names(profile, brief, memory_requested=request.memory)
            meeting_context = MeetingContext(
                schema_version="meeting_context/v1",
                brief=brief,
                brief_sha256=brief_digest(brief),
                persona_sha256=persona_digest(profile),
            )
        if not request.twin_name:
            config = replace(config, twin_name=profile.display_name)
    return profile, brief, meeting_context, config


async def _load_brief(path: Path, config: SessionConfig) -> MeetingBrief:
    try:
        return await asyncio.to_thread(
            load_meeting_brief,
            path,
            owner_id=config.owner_id,
            twin_id=config.twin_id,
            meeting_id=config.meeting_id,
        )
    except OSError as error:
        raise FakeYouError(ErrorCode.MEETING_BRIEF_UNREADABLE, reason=error.strerror) from None
    except ValidationError as error:
        kinds = sorted(
            {item["type"] for item in error.errors(include_url=False, include_input=False)}
        )
        raise FakeYouError(
            ErrorCode.MEETING_BRIEF_SCHEMA_INVALID,
            count=error.error_count(),
            kinds=", ".join(kinds),
        ) from None


def prepare_session_paths(config: SessionConfig, settings: Settings) -> tuple[SessionConfig, Path]:
    layout = MeetingDir(settings.paths.record_dir, config.meeting_id)
    record_path = layout.record
    config = replace(
        config,
        participant_evidence_path=layout.participants,
        per_stream_enabled=settings.conversation.per_stream_enabled,
        perception_enabled=settings.perception.enabled,
    )
    return config, record_path


async def guard_join(prepared: PreparedJoin, ports: JoinPorts) -> tuple[bool, bool]:
    request = prepared.request
    session_state = describe_session(request.settings.paths.journal_dir, prepared.config.meeting_id)
    if session_state == "unfinished" and not request.resume:
        raise FakeYouError(
            ErrorCode.MEETING_UNFINISHED_SESSION, meeting_id=prepared.config.meeting_id
        )
    if session_state == "finished" and request.resume:
        raise FakeYouError(ErrorCode.MEETING_ALREADY_ENDED, meeting_id=prepared.config.meeting_id)
    resuming = request.resume and (
        session_state == "unfinished" or await _has_live_thread(prepared.config, ports)
    )
    context_reused = await _settle_meeting_context(
        prepared.record_path, prepared.config, prepared.meeting_context
    )
    return resuming, context_reused


async def _settle_meeting_context(
    record_path: Path, config: SessionConfig, context: MeetingContext | None
) -> bool:
    path = context_path(record_path)
    try:
        existing = read_meeting_context(path)
    except (OSError, ValueError):
        raise FakeYouError(ErrorCode.MEETING_CONTEXT_UNREADABLE, path=path) from None
    if context is None:
        if existing is not None:
            raise FakeYouError(ErrorCode.MEETING_STARTED_WITH_BRIEF, meeting_id=config.meeting_id)
        return False
    if existing is not None:
        if existing != context:
            raise FakeYouError(ErrorCode.MEETING_CONTEXT_DIFFERS)
        return True
    if await asyncio.to_thread(_has_entries, record_path):
        raise FakeYouError(
            ErrorCode.MEETING_HAS_CONVERSATION_WITHOUT_CONTEXT, meeting_id=config.meeting_id
        )
    try:
        write_meeting_context(path, context)
    except (OSError, ValueError) as error:
        raise FakeYouError(
            ErrorCode.MEETING_CONTEXT_NOT_WRITTEN, reason=type(error).__name__
        ) from None
    return False


def _has_entries(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


async def _has_live_thread(config: SessionConfig, ports: JoinPorts) -> bool:
    if config.database_url is None:
        return False
    try:
        async with ports.open_checkpointer(
            config.database_url,
            connect_timeout_seconds=config.database_connect_timeout_seconds,
            statement_timeout_seconds=config.database_statement_timeout_seconds,
            keepalive_idle_seconds=config.database_keepalive_idle_seconds,
        ) as saver:
            saved = await saver.aget_tuple(
                {
                    "configurable": {
                        "thread_id": meeting_thread_id(config.twin_id, config.meeting_id)
                    }
                }
            )
    except FakeYouError as error:
        reason = trim_detail(f"{type(error).__name__}: {error}") or type(error).__name__
        raise FakeYouError(
            ErrorCode.MEETING_LIFECYCLE_CHECKPOINT_UNCHECKABLE, reason=reason
        ) from None
    if saved is None:
        return False
    return saved.checkpoint["channel_values"].get("phase") not in TERMINAL_PHASES
