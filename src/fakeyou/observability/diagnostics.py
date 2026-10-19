from dataclasses import dataclass

from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal


@dataclass(frozen=True, slots=True)
class Diagnostics:
    faults: FaultReporter | None = None
    journal: Journal | None = None


DEFAULT_DIAGNOSTICS = Diagnostics()
