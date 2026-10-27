from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Literal

from fakeyou.session.contracts import (
    AudioStreamObserved,
    CallState,
    CallStateObserved,
    ChatObserved,
    OwnerControlObserved,
    ParticipantCountObserved,
    PlaybackObserved,
    RosterObserved,
    ScreenObserved,
    SessionEvent,
    Source,
    SourceHealthObserved,
    SourceStatus,
    TranscriptObserved,
    VoiceTrackObserved,
)

Presence = Literal["present", "absent", "unknown"]
RefKind = Literal["track", "stream", "device", "twin", "unknown"]
Channel = Literal["speech", "chat"]
TERMINAL_CALL_STATES: frozenset[CallState] = frozenset({"left", "removed", "ended"})
IDENTITY_SOURCES: frozenset[Source] = frozenset(
    {"meet_protocol", "meet_dom", "speaker_registry", "stt"}
)


@dataclass(frozen=True, slots=True)
class Limits:
    dialogue_lines: int = 50
    interims: int = 32

    def __post_init__(self) -> None:
        if self.dialogue_lines < 1 or self.interims < 1:
            raise ValueError("limits must be at least 1")


@dataclass(frozen=True, slots=True)
class Ref:
    kind: RefKind
    id: str = ""


UNKNOWN = Ref("unknown")
TWIN = Ref("twin")


@dataclass(frozen=True, slots=True)
class EndpointState:
    device_id: str
    display_name: str
    presence: Presence
    parent_device_id: str | None
    current_user: bool
    host: bool


@dataclass(frozen=True, slots=True)
class TrackState:
    track_id: str
    device_id: str | None
    stream_id: str | None
    enrolled_person: str | None


@dataclass(frozen=True, slots=True)
class ScreenState:
    title: str | None
    reading_ref: str | None
    meeting_content: bool
    since_seq: int


@dataclass(frozen=True, slots=True)
class WorldState:
    endpoints: Mapping[str, EndpointState] = field(default_factory=dict)
    streams: Mapping[str, str] = field(default_factory=dict)
    tracks: Mapping[str, TrackState] = field(default_factory=dict)
    heard: Mapping[Ref, int] = field(default_factory=dict)
    roster_synced: bool = False
    dom_remote: int | None = None
    screen: ScreenState | None = None
    call_state: CallState | None = None
    browser_epoch: int = 0


@dataclass(frozen=True, slots=True)
class DialogueLine:
    seq: int
    channel: Channel
    speaker: Ref
    text: str
    source_id: str | None = None
    backlog: bool = False
    utterance_id: str | None = None
    sentence_index: int | None = None
    interrupted: bool = False


@dataclass(frozen=True, slots=True)
class DialogueState:
    lines: tuple[DialogueLine, ...] = ()
    interims: Mapping[Ref, str] = field(default_factory=dict)
    floor_holder: Ref | None = None
    twin_playing: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class AgendaState:
    paused: bool = False
    owner_leave: bool = False


@dataclass(frozen=True, slots=True)
class HealthState:
    sources: Mapping[Source, SourceStatus] = field(default_factory=dict)
    late_events: int = 0
    ignored_events: int = 0
    dropped_events: int = 0
    saturated: bool = False


@dataclass(frozen=True, slots=True)
class Revisions:
    presence: int = 0
    floor: int = 0
    screen: int = 0
    dialogue: int = 0
    agenda: int = 0
    health: int = 0


@dataclass(frozen=True, slots=True)
class SessionState:
    seq: int = 0
    world: WorldState = field(default_factory=WorldState)
    dialogue: DialogueState = field(default_factory=DialogueState)
    agenda: AgendaState = field(default_factory=AgendaState)
    health: HealthState = field(default_factory=HealthState)
    revisions: Revisions = field(default_factory=Revisions)

    @property
    def terminal(self) -> bool:
        return self.world.call_state in TERMINAL_CALL_STATES


def _bump(revisions: Revisions, *domains: str) -> Revisions:
    return replace(revisions, **{domain: getattr(revisions, domain) + 1 for domain in domains})


def _changed(state: SessionState, updated: SessionState, *domains: str) -> SessionState:
    if updated == state:
        return state
    return replace(updated, revisions=_bump(state.revisions, *domains))


def speaker_ref(observation: TranscriptObserved) -> Ref:
    if observation.track_id is not None:
        return Ref("track", observation.track_id)
    if observation.stream_id is not None:
        return Ref("stream", observation.stream_id)
    return UNKNOWN


def _append(
    lines: tuple[DialogueLine, ...], line: DialogueLine, keep: int
) -> tuple[DialogueLine, ...]:
    return (*lines, line)[-keep:]


def presenter_device(world: WorldState) -> str | None:
    endpoints = world.endpoints
    for endpoint in endpoints.values():
        parent = endpoints.get(endpoint.parent_device_id or "")
        if (
            endpoint.presence == "present"
            and parent is not None
            and not endpoint.current_user
            and not parent.current_user
        ):
            return parent.device_id
    return None


def _roster(state: SessionState, observation: RosterObserved) -> SessionState:
    endpoints = dict(state.world.endpoints)
    for endpoint in observation.endpoints:
        endpoints[endpoint.device_id] = EndpointState(
            device_id=endpoint.device_id,
            display_name=endpoint.display_name,
            presence="present" if endpoint.present else "absent",
            parent_device_id=endpoint.parent_device_id,
            current_user=endpoint.current_user,
            host=endpoint.host,
        )
    world = replace(
        state.world,
        endpoints=endpoints,
        roster_synced=state.world.roster_synced or observation.synced,
    )
    if presenter_device(world) != presenter_device(state.world):
        return _changed(state, replace(state, world=world), "presence", "screen")
    return _changed(state, replace(state, world=world), "presence")


def _voice_track(state: SessionState, observation: VoiceTrackObserved) -> SessionState:
    known = state.world.tracks.get(observation.track_id)
    track = TrackState(
        track_id=observation.track_id,
        device_id=observation.device_id
        if observation.device_id is not None
        else (known.device_id if known is not None else None),
        stream_id=observation.stream_id
        if observation.stream_id is not None
        else (known.stream_id if known is not None else None),
        enrolled_person=observation.enrolled_person
        if observation.enrolled_person is not None
        else (known.enrolled_person if known is not None else None),
    )
    world = replace(state.world, tracks={**state.world.tracks, observation.track_id: track})
    return _changed(state, replace(state, world=world), "presence")


def _heard(world: WorldState, seq: int, observation: TranscriptObserved) -> WorldState:
    refs = [Ref("stream", observation.stream_id)] if observation.stream_id is not None else []
    tracks = dict(world.tracks)
    if observation.track_id is not None:
        refs.append(Ref("track", observation.track_id))
        known = tracks.get(observation.track_id)
        tracks[observation.track_id] = TrackState(
            track_id=observation.track_id,
            device_id=known.device_id if known is not None else None,
            stream_id=observation.stream_id
            if observation.stream_id is not None
            else (known.stream_id if known is not None else None),
            enrolled_person=known.enrolled_person if known is not None else None,
        )
    return replace(world, tracks=tracks, heard={**world.heard, **dict.fromkeys(refs, seq)})


def _transcript(
    state: SessionState, seq: int, observation: TranscriptObserved, limits: Limits
) -> SessionState:
    ref = speaker_ref(observation)
    dialogue = state.dialogue
    if not observation.final:
        interims = {key: text for key, text in dialogue.interims.items() if key != ref}
        interims[ref] = observation.text
        kept = dict(list(interims.items())[-limits.interims :])
        holder_changed = dialogue.floor_holder != ref
        updated = replace(dialogue, interims=kept, floor_holder=ref)
        revisions = _bump(state.revisions, "floor") if holder_changed else state.revisions
        return replace(state, dialogue=updated, revisions=revisions)
    world = _heard(state.world, seq, observation)
    newly_heard = (
        world.heard.keys() != state.world.heard.keys() or world.tracks != state.world.tracks
    )
    line = DialogueLine(
        seq=seq,
        channel="speech",
        speaker=ref,
        text=observation.text,
        source_id=observation.transcript_id,
    )
    updated = replace(
        dialogue,
        lines=_append(dialogue.lines, line, limits.dialogue_lines),
        interims={
            key: text for key, text in dialogue.interims.items() if key not in (ref, UNKNOWN)
        },
        floor_holder=None if dialogue.floor_holder in (ref, UNKNOWN) else dialogue.floor_holder,
    )
    domains = ("dialogue", "floor", "presence") if newly_heard else ("dialogue", "floor")
    return replace(
        state,
        world=world,
        dialogue=updated,
        revisions=_bump(state.revisions, *domains),
    )


def _chat(state: SessionState, seq: int, observation: ChatObserved, limits: Limits) -> SessionState:
    line = DialogueLine(
        seq=seq,
        channel="chat",
        speaker=TWIN if observation.own else Ref("device", observation.device_id),
        text=observation.text,
        source_id=observation.message_id,
        backlog=observation.backlog,
    )
    dialogue = replace(
        state.dialogue, lines=_append(state.dialogue.lines, line, limits.dialogue_lines)
    )
    return replace(state, dialogue=dialogue, revisions=_bump(state.revisions, "dialogue"))


def _screen(state: SessionState, seq: int, observation: ScreenObserved) -> SessionState:
    current = state.world.screen
    screen = ScreenState(
        title=observation.title,
        reading_ref=observation.reading_ref,
        meeting_content=observation.meeting_content,
        since_seq=seq,
    )
    if current is not None and replace(current, since_seq=seq) == screen:
        return state
    return replace(
        state,
        world=replace(state.world, screen=screen),
        revisions=_bump(state.revisions, "screen"),
    )


def _call_state(state: SessionState, observation: CallStateObserved) -> SessionState:
    world = state.world
    if observation.browser_epoch > world.browser_epoch:
        world = WorldState(
            tracks={
                track_id: replace(track, device_id=None, stream_id=None)
                for track_id, track in world.tracks.items()
            },
            heard={ref: seq for ref, seq in world.heard.items() if ref.kind == "track"},
            screen=world.screen,
            browser_epoch=observation.browser_epoch,
        )
    world = replace(world, call_state=observation.state)
    return _changed(state, replace(state, world=world), "presence", "floor", "health")


def _source_health(state: SessionState, observation: SourceHealthObserved) -> SessionState:
    health = replace(
        state.health, sources={**state.health.sources, observation.source: observation.status}
    )
    domains = ("health", "presence") if observation.source in IDENTITY_SOURCES else ("health",)
    return _changed(state, replace(state, health=health), *domains)


def _playback(
    state: SessionState, seq: int, observation: PlaybackObserved, limits: Limits
) -> SessionState:
    dialogue = state.dialogue
    if observation.phase == "started":
        playing = dialogue.twin_playing | {observation.utterance_id}
        return _changed(
            state, replace(state, dialogue=replace(dialogue, twin_playing=playing)), "floor"
        )
    playing = dialogue.twin_playing - {observation.utterance_id}
    lines = dialogue.lines
    if observation.phase in ("finished", "interrupted"):
        lines = _append(
            lines,
            DialogueLine(
                seq=seq,
                channel="speech",
                speaker=TWIN,
                text=observation.text,
                utterance_id=observation.utterance_id,
                sentence_index=observation.sentence_index,
                interrupted=observation.phase == "interrupted",
            ),
            limits.dialogue_lines,
        )
    updated = replace(dialogue, lines=lines, twin_playing=playing)
    return _changed(state, replace(state, dialogue=updated), "floor", "dialogue")


def _owner_control(state: SessionState, observation: OwnerControlObserved) -> SessionState:
    agenda = state.agenda
    match observation.command:
        case "pause":
            agenda = replace(agenda, paused=True)
        case "resume":
            agenda = replace(agenda, paused=False)
        case "leave":
            agenda = replace(agenda, owner_leave=True)
        case "stop_playback":
            return state
    return _changed(state, replace(state, agenda=agenda), "agenda")


def _participant_count(state: SessionState, observation: ParticipantCountObserved) -> SessionState:
    world = replace(state.world, dom_remote=observation.remote)
    return _changed(state, replace(state, world=world), "presence")


def _audio_stream(state: SessionState, observation: AudioStreamObserved) -> SessionState:
    world = replace(
        state.world, streams={**state.world.streams, observation.stream_id: observation.device_id}
    )
    return _changed(state, replace(state, world=world), "presence")


PresenceObservation = (
    RosterObserved
    | ParticipantCountObserved
    | AudioStreamObserved
    | VoiceTrackObserved
    | CallStateObserved
    | SourceHealthObserved
)


def _observe_presence(state: SessionState, observation: PresenceObservation) -> SessionState:
    match observation:
        case RosterObserved():
            return _roster(state, observation)
        case ParticipantCountObserved():
            return _participant_count(state, observation)
        case AudioStreamObserved():
            return _audio_stream(state, observation)
        case VoiceTrackObserved():
            return _voice_track(state, observation)
        case CallStateObserved():
            return _call_state(state, observation)
        case SourceHealthObserved():
            return _source_health(state, observation)


def _observe(state: SessionState, seq: int, event: SessionEvent, limits: Limits) -> SessionState:
    observation = event.observation
    match observation:
        case TranscriptObserved():
            return _transcript(state, seq, observation, limits)
        case ChatObserved():
            return _chat(state, seq, observation, limits)
        case PlaybackObserved():
            return _playback(state, seq, observation, limits)
        case ScreenObserved():
            return _screen(state, seq, observation)
        case OwnerControlObserved():
            return _owner_control(state, observation)
        case _:
            return _observe_presence(state, observation)


DEFAULT_LIMITS = Limits()


def reduce(
    state: SessionState, event: SessionEvent, *, late: bool = False, limits: Limits = DEFAULT_LIMITS
) -> SessionState:
    seq = state.seq + 1
    if state.terminal and not isinstance(event.observation, PlaybackObserved):
        health = replace(state.health, ignored_events=state.health.ignored_events + 1)
        return replace(state, seq=seq, health=health)
    reduced = replace(_observe(state, seq, event, limits), seq=seq)
    if not late:
        return reduced
    health = replace(reduced.health, late_events=reduced.health.late_events + 1)
    return replace(reduced, health=health)
