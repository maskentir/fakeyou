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
class StreamAssignment:
    stream_id: str
    role: StreamRole
    device_id: str


class MeetRoster:
    def __init__(self) -> None:
        self._users: dict[str, MeetUser] = {}
        self._outputs: dict[tuple[str, OutputKind], DeviceOutput] = {}

    @property
    def own_device_ids(self) -> frozenset[str]:
        return frozenset(device for device, user in self._users.items() if user.current_user)

    def user(self, device_id: str) -> MeetUser | None:
        return self._users.get(device_id)

    def name_of(self, device_id: str) -> str:
        user = self._users.get(device_id)
        return user.name if user is not None else ""

    def apply_users(self, users: Iterable[MeetUser], *, complete: bool) -> list[RosterChange]:
        changes: list[RosterChange] = []
        seen: set[str] = set()
        for user in users:
            seen.add(user.device_id)
            changes.extend(self._upsert(user))
        if complete:
            for device_id, known in tuple(self._users.items()):
                if device_id not in seen and known.present:
                    changes.extend(self._upsert(known.model_copy(update={"present": False})))
        return changes

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
        if before is not None and not user.name:
            merged = user.model_copy(update={"name": before.name})
        self._users[user.device_id] = merged
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
