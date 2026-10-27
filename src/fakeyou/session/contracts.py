from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, NonNegativeInt

SCHEMA_VERSION: Literal[1] = 1

Source = Literal[
    "stt",
    "meet_protocol",
    "meet_dom",
    "speaker_registry",
    "vision",
    "playback",
    "browser",
    "owner_control",
    "scheduler",
    "executor",
    "model",
    "memory",
]
PrivacyClass = Literal["room", "owner_only", "internal", "ephemeral"]
SourceStatus = Literal["healthy", "stale", "down"]
CallState = Literal["in_call", "left", "removed", "ended"]
PlaybackPhase = Literal["started", "finished", "interrupted", "dropped", "failed"]
OwnerCommand = Literal["stop_playback", "leave", "pause", "resume"]
NonEmpty = Annotated[str, Field(min_length=1)]


class Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Endpoint(Contract):
    device_id: NonEmpty
    display_name: str = ""
    present: bool
    parent_device_id: str | None = None
    current_user: bool = False
    host: bool = False


class RosterObserved(Contract):
    kind: Literal["roster"] = "roster"
    endpoints: tuple[Endpoint, ...] = ()
    synced: bool = False


class ParticipantCountObserved(Contract):
    kind: Literal["participant_count"] = "participant_count"
    count: NonNegativeInt


class AudioStreamObserved(Contract):
    kind: Literal["audio_stream"] = "audio_stream"
    device_id: NonEmpty
    stream_id: NonEmpty
    disabled: bool = False


class VoiceTrackObserved(Contract):
    kind: Literal["voice_track"] = "voice_track"
    track_id: NonEmpty
    stream_id: str | None = None
    enrolled_person: str | None = None


class TranscriptObserved(Contract):
    kind: Literal["transcript"] = "transcript"
    transcript_id: NonEmpty
    text: str
    final: bool
    stream_id: str | None = None
    track_id: str | None = None
    language: str | None = None


class ChatObserved(Contract):
    kind: Literal["chat"] = "chat"
    message_id: NonEmpty
    device_id: NonEmpty
    text: str
    own: bool = False
    backlog: bool = False


class ScreenObserved(Contract):
    kind: Literal["screen"] = "screen"
    presenter_device_id: str | None = None
    title: str | None = None
    reading_ref: str | None = None
    meeting_content: bool = True


class CallStateObserved(Contract):
    kind: Literal["call_state"] = "call_state"
    state: CallState
    browser_epoch: NonNegativeInt


class SourceHealthObserved(Contract):
    kind: Literal["source_health"] = "source_health"
    source: Source
    status: SourceStatus


class PlaybackObserved(Contract):
    kind: Literal["playback"] = "playback"
    utterance_id: NonEmpty
    sentence_index: NonNegativeInt
    phase: PlaybackPhase
    text: str


class OwnerControlObserved(Contract):
    kind: Literal["owner_control"] = "owner_control"
    command: OwnerCommand


Observation = Annotated[
    RosterObserved
    | ParticipantCountObserved
    | AudioStreamObserved
    | VoiceTrackObserved
    | TranscriptObserved
    | ChatObserved
    | ScreenObserved
    | CallStateObserved
    | SourceHealthObserved
    | PlaybackObserved
    | OwnerControlObserved,
    Field(discriminator="kind"),
]


class SessionEvent(Contract):
    event_id: NonEmpty
    session_id: NonEmpty
    attempt_id: NonEmpty
    source: Source
    received_at: AwareDatetime
    observation: Observation
    source_seq: NonNegativeInt | None = None
    observed_at: AwareDatetime | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    privacy_class: PrivacyClass = "room"
    schema_version: Literal[1] = SCHEMA_VERSION
