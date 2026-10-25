import os
import shutil
import sys

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.meeting_request import MeetingRequest, validate_meeting_request
from fakeyou.foundation.invariants import AGENT_SINK_NAME, MEETING_MONITOR_SOURCE_NAME
from fakeyou.orchestration.join_types import JoinRequest
from fakeyou.orchestration.providers import memory_config


def parec_available() -> bool:
    return shutil.which("parec") is not None


def _require_audio_platform() -> None:
    if (
        sys.platform != "linux"
        or os.environ.get("PULSE_SOURCE") != MEETING_MONITOR_SOURCE_NAME
        or os.environ.get("PULSE_SINK") != AGENT_SINK_NAME
        or not parec_available()
    ):
        raise FakeYouError(ErrorCode.MEETING_AUDIO_REQUIRES_WORKER)


def preflight_join(request: JoinRequest) -> None:
    settings = request.settings
    validate_meeting_request(
        MeetingRequest(
            link=request.link,
            lobby_timeout=request.lobby_timeout,
            duration=request.duration,
            initial_speech=request.initial_speech,
            audio=request.audio_enabled,
            conversation=request.conversation_enabled,
            memory=request.memory,
            persona_path=request.identity.persona_path,
            brief_path=request.brief_path,
            meeting_id=request.meeting_id,
            wake_phrase=request.wake_phrase,
            barge_in=request.barge_in,
            owner_id=request.identity.owner_id,
            twin_id=request.identity.twin_id,
        )
    )
    if request.conversation_enabled:
        settings.require_roles("reply", "reply_fallback", "reply_strong")
    if request.memory:
        memory_config(settings)
    if request.audio_enabled:
        _require_audio_platform()
        settings.require_roles("stt", "tts", "tts_fallback")
