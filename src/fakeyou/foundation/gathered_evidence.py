from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Literal, override

from fakeyou.foundation.publication import EvidenceBinding


@dataclass(frozen=True, slots=True, eq=False)
class GatheredEvidence(Mapping[str, object]):
    payloads: Mapping[str, object]
    gather_attempt_id: str
    provenance: tuple[EvidenceBinding, ...]
    scope: Literal["current", "historical"] = "current"

    @override
    def __getitem__(self, key: str) -> object:
        return self.payloads[key]

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(self.payloads)

    @override
    def __len__(self) -> int:
        return len(self.payloads)
