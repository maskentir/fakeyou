from collections import Counter
from dataclasses import dataclass
from typing import Literal

from fakeyou.session.world import TWIN, Ref, SessionState, TrackState, WorldState

EdgeStatus = Literal["confirmed", "probable", "unknown", "conflicting", "expired"]
PresenceStatus = Literal["complete", "incomplete", "stale", "not_in_call"]
UNHEALTHY = frozenset({"stale", "down"})


@dataclass(frozen=True, slots=True)
class Participant:
    device_id: str
    display_claim: str
    host: bool
    voices: tuple[str, ...]
    enrolled: tuple[str, ...]
    claim_conflict: bool


@dataclass(frozen=True, slots=True)
class Attribution:
    device_id: str | None
    display_claim: str | None
    enrolled_person: str | None
    status: EdgeStatus


@dataclass(frozen=True, slots=True)
class PresenceFact:
    status: PresenceStatus
    confirmed_min: int
    plausible_max: int | None
    unresolved: int
    shared_endpoints: int
    drift: bool
    twin_identified: bool
    participants: tuple[Participant, ...]

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    @property
    def sole_human_responder(self) -> bool:
        return self.complete and self.confirmed_min == 1


def _twin_devices(world: WorldState) -> frozenset[str]:
    return frozenset(
        device for device, endpoint in world.endpoints.items() if endpoint.current_user
    )


def _human_devices(world: WorldState) -> tuple[str, ...]:
    return tuple(
        sorted(
            device
            for device, endpoint in world.endpoints.items()
            if endpoint.presence == "present"
            and endpoint.parent_device_id is None
            and not endpoint.current_user
        )
    )


def _track_device(world: WorldState, track: TrackState) -> str | None:
    if track.device_id is not None:
        return track.device_id
    return world.streams.get(track.stream_id) if track.stream_id is not None else None


def _voice_key(world: WorldState, ref: Ref) -> str | None:
    if ref.kind == "stream":
        return f"stream:{ref.id}"
    if ref.kind == "track":
        track = world.tracks.get(ref.id)
        if track is not None and track.device_id is not None:
            return f"device:{track.device_id}"
        if track is not None and track.stream_id is not None:
            return f"stream:{track.stream_id}"
        return f"track:{ref.id}"
    return None


def _voice_device(world: WorldState, key: str) -> str | None:
    kind, _, value = key.partition(":")
    if kind == "device":
        return value
    return world.streams.get(value) if kind == "stream" else None


def attribute(state: SessionState, ref: Ref) -> Attribution:
    world = state.world
    if ref == TWIN:
        return Attribution(None, None, None, "confirmed")
    if ref.kind == "device":
        endpoint = world.endpoints.get(ref.id)
        return Attribution(
            ref.id,
            endpoint.display_name if endpoint is not None else None,
            None,
            "confirmed" if endpoint is not None and endpoint.presence == "present" else "unknown",
        )
    track = world.tracks.get(ref.id) if ref.kind == "track" else None
    enrolled = track.enrolled_person if track is not None else None
    key = _voice_key(world, ref)
    device = _voice_device(world, key) if key is not None else None
    endpoint = world.endpoints.get(device) if device is not None else None
    if endpoint is None:
        return Attribution(None, None, enrolled, "unknown")
    shared = _shared(world).get(endpoint.device_id, 0) > 1
    return Attribution(
        endpoint.device_id,
        endpoint.display_name,
        enrolled,
        "probable" if shared else "confirmed",
    )


def _shared(world: WorldState) -> Counter[str]:
    voices: Counter[str] = Counter()
    for track in world.tracks.values():
        if Ref("track", track.track_id) not in world.heard:
            continue
        device = _track_device(world, track)
        if device is not None:
            voices[device] += 1
    return voices


def _unresolved(state: SessionState, humans: frozenset[str], twin: frozenset[str]) -> int:
    world = state.world
    keys: set[str] = set()
    for ref in world.heard:
        key = _voice_key(world, ref)
        if key is None:
            continue
        device = _voice_device(world, key)
        if device in twin or device in humans:
            continue
        keys.add(key)
    for line in state.dialogue.lines:
        if line.speaker.kind == "device" and line.speaker.id not in humans | twin:
            keys.add(f"device:{line.speaker.id}")
    return len(keys)


def _participants(world: WorldState, humans: tuple[str, ...]) -> tuple[Participant, ...]:
    claims = Counter(
        world.endpoints[device].display_name.casefold()
        for device in humans
        if world.endpoints[device].display_name
    )
    participants = []
    for device in humans:
        endpoint = world.endpoints[device]
        tracks = sorted(
            (track for track in world.tracks.values() if _track_device(world, track) == device),
            key=lambda track: track.track_id,
        )
        participants.append(
            Participant(
                device_id=device,
                display_claim=endpoint.display_name,
                host=endpoint.host,
                voices=tuple(track.track_id for track in tracks),
                enrolled=tuple(
                    sorted({track.enrolled_person for track in tracks if track.enrolled_person})
                ),
                claim_conflict=claims[endpoint.display_name.casefold()] > 1,
            )
        )
    return tuple(participants)


def presence(state: SessionState) -> PresenceFact:
    world = state.world
    humans = _human_devices(world)
    twin = _twin_devices(world)
    participants = _participants(world, humans)
    shared = _shared(world)
    unresolved = _unresolved(state, frozenset(humans), twin)
    shared_endpoints = sum(1 for device in humans if shared.get(device, 0) > 1)
    confirmed_min = sum(max(1, shared.get(device, 0)) for device in humans)
    drift = world.dom_remote is not None and world.dom_remote != len(humans)
    protocol = state.health.sources.get("meet_protocol")
    if world.call_state != "in_call":
        status: PresenceStatus = "not_in_call"
    elif protocol in UNHEALTHY:
        status = "stale"
    elif world.roster_synced and twin and unresolved == 0 and shared_endpoints == 0 and not drift:
        status = "complete"
    else:
        status = "incomplete"
    if status == "stale":
        confirmed_min = 0
    elif not humans and unresolved:
        confirmed_min = 1
    return PresenceFact(
        status=status,
        confirmed_min=confirmed_min,
        plausible_max=confirmed_min if status == "complete" else None,
        unresolved=unresolved,
        shared_endpoints=shared_endpoints,
        drift=drift,
        twin_identified=bool(twin),
        participants=participants,
    )
