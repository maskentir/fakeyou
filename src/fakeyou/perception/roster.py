from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from fakeyou.perception.meet_protocol import DeviceOutput, MeetUser, OutputKind

RosterChangeKind = Literal[
    "joined",
    "left",
    "presenting_started",
    "presenting_stopped",
    "camera_on",
    "camera_off",
]
StreamRole = Literal["screen", "camera"]


@dataclass(frozen=True, slots=True)
class RosterChange:
    kind: RosterChangeKind
    device_id: str
    name: str


@dataclass(frozen=True, slots=True)
class MeetingState:
    people: dict[str, str]
    presenter: str | None
    cameras_on: frozenset[str]
    synced: bool

    @property
    def remote_count(self) -> int:
        return len(self.people)


@dataclass(frozen=True, slots=True)
class StreamAssignment:
    stream_id: str
    role: StreamRole
    device_id: str


class MeetRoster:
    def __init__(self) -> None:
        self._users: dict[str, MeetUser] = {}
        self._outputs: dict[tuple[str, OutputKind], DeviceOutput] = {}
        self._synced = False
        self._own_updates: list[str] = []
        self._retired: set[str] = set()
        self._awaiting_snapshot = False

    def mark_synced(self) -> None:
        self._synced = True

    def begin_epoch(self) -> None:
        self._retired.update(self._own_updates)
        self._own_updates.clear()
        self._synced = False
        self._awaiting_snapshot = True

    def apply_snapshot(self, users: Iterable[MeetUser]) -> list[RosterChange]:
        listed = list(users)
        changes = self.apply_users(listed)
        if not self._awaiting_snapshot:
            return changes
        self._awaiting_snapshot = False
        seen = {user.device_id for user in listed}
        for device_id, user in tuple(self._users.items()):
            if user.present and device_id not in seen and not self._is_own(device_id):
                changes += self._upsert(user.model_copy(update={"present": False}))
        return changes

    def state(self) -> MeetingState:
        people = self.people()
        return MeetingState(
            people=people,
            presenter=self.presenter(),
            cameras_on=frozenset(
                device_id
                for (device_id, kind), output in self._outputs.items()
                if kind == "video" and not output.disabled and device_id in people
            ),
            synced=self._synced,
        )

    def presenter(self) -> str | None:
        for user in self._users.values():
            parent = user.parent_device_id
            if user.present and parent is not None and not self._is_own(user.device_id):
                return self.name_of(parent) or None
        return None

    @property
    def own_device_ids(self) -> frozenset[str]:
        return frozenset(device for device, user in self._users.items() if user.current_user)

    def own_user(self) -> MeetUser | None:
        own = [self._users[device_id] for device_id in reversed(self._own_updates)]
        return next((user for user in own if user.present), own[0] if own else None)

    def users(self) -> tuple[MeetUser, ...]:
        return tuple(self._users.values())

    @property
    def synced(self) -> bool:
        return self._synced

    def user(self, device_id: str) -> MeetUser | None:
        return self._users.get(device_id)

    def name_of(self, device_id: str) -> str:
        user = self._users.get(device_id)
        return user.name if user is not None else ""

    def apply_users(self, users: Iterable[MeetUser]) -> list[RosterChange]:
        return [change for user in users for change in self._upsert(user)]

    def apply_outputs(self, outputs: Iterable[DeviceOutput]) -> list[RosterChange]:
        changes: list[RosterChange] = []
        for output in outputs:
            key = (output.device_id, output.kind)
            before = self._outputs.get(key)
            self._outputs[key] = output
            if output.kind != "video" or self._is_screen(output.device_id):
                continue
            if self._is_own(output.device_id):
                continue
            switched = before is not None and before.disabled != output.disabled
            if switched or (before is None and not output.disabled):
                kind: RosterChangeKind = "camera_off" if output.disabled else "camera_on"
                changes.append(RosterChange(kind, output.device_id, self.name_of(output.device_id)))
        return changes

    def people(self) -> dict[str, str]:
        return {
            device_id: user.name
            for device_id, user in self._users.items()
            if user.present and not user.is_screen and not self._is_own(device_id)
        }

    def audio_sources(self) -> dict[int, str]:
        people = self.people()
        return {
            int(output.stream_id): device_id
            for (device_id, kind), output in self._outputs.items()
            if kind == "audio" and device_id in people and output.stream_id.isdigit()
        }

    def assignments(self) -> list[StreamAssignment]:
        assigned: list[StreamAssignment] = []
        for (device_id, kind), output in self._outputs.items():
            if kind != "video" or output.disabled or self._is_own(device_id):
                continue
            user = self._users.get(device_id)
            if user is not None and user.parent_device_id is not None:
                assigned.append(StreamAssignment(output.stream_id, "screen", user.parent_device_id))
            else:
                assigned.append(StreamAssignment(output.stream_id, "camera", device_id))
        return assigned

    def assignment_for(self, stream_id: str) -> StreamAssignment | None:
        return next((item for item in self.assignments() if item.stream_id == stream_id), None)

    def _is_screen(self, device_id: str) -> bool:
        user = self._users.get(device_id)
        return user is not None and user.is_screen

    def _is_own(self, device_id: str) -> bool:
        user = self._users.get(device_id)
        if user is None:
            return False
        if user.current_user:
            return True
        parent = user.parent_device_id
        return parent is not None and parent in self.own_device_ids

    def _upsert(self, user: MeetUser) -> list[RosterChange]:
        before = self._users.get(user.device_id)
        merged = user
        if before is not None:
            merged = user.model_copy(
                update={
                    "name": user.name or before.name,
                    "current_user": user.current_user or before.current_user,
                }
            )
        self._users[user.device_id] = merged
        if merged.current_user and not merged.is_screen and merged.device_id not in self._retired:
            if merged.device_id in self._own_updates:
                self._own_updates.remove(merged.device_id)
            self._own_updates.append(merged.device_id)
        if self._is_own(merged.device_id):
            return []
        was_present = before is not None and before.present
        if merged.present == was_present:
            return []
        if merged.parent_device_id is not None:
            parent = merged.parent_device_id
            kind: RosterChangeKind = (
                "presenting_started" if merged.present else "presenting_stopped"
            )
            return [RosterChange(kind, parent, self.name_of(parent))]
        return [RosterChange("joined" if merged.present else "left", merged.device_id, merged.name)]
