from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fakeyou.perception.evidence import ProviderAttempt, SnapshotRef
from fakeyou.perception.frames import compare_screens
from fakeyou.perception.inspector import VisualInspection
from fakeyou.perception.percepts import Keyframe


def observations_match(left: Keyframe, right: Keyframe) -> bool:
    return compare_screens(left, right) in {"same", "minor"}


@dataclass(slots=True)
class AnswerEntry:
    question: str
    inspection: VisualInspection
    ref: SnapshotRef
    keyframe: Keyframe
    stored_at: float
    attempt: ProviderAttempt | None = None


class StaleEvidence(Exception):
    pass


@dataclass(slots=True)
class AnswerCache:
    entries: dict[str, AnswerEntry] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def lock_for(self, participant_id: str) -> asyncio.Lock:
        lock = self.locks.get(participant_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[participant_id] = lock
        return lock

    def get(self, participant_id: str) -> AnswerEntry | None:
        return self.entries.get(participant_id)

    def put(self, participant_id: str, entry: AnswerEntry) -> None:
        self.entries[participant_id] = entry

    def drop(self, participant_id: str) -> None:
        self.entries.pop(participant_id, None)

    def clear(self) -> None:
        self.entries.clear()
