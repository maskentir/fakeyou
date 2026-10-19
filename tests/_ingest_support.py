from contextlib import AbstractContextManager, nullcontext
from typing import Any

import fakeyou.orchestration.providers as providers
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.output import emit_error, emit_json
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.memory.ingest_service import IngestPorts, IngestRequest, run_recorded_ingest
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import OperatorIdentity


def _no_stop_signals(_stop: object, _journal: object) -> AbstractContextManager[None]:
    return nullcontext()


def _print_summary(summary: Any) -> None:
    emit_json(
        {
            "meeting_id": summary.meeting_id,
            "windows": summary.windows,
            "resumed_from": summary.resumed_from,
            "ingested": summary.ingested,
            "facts": summary.facts,
            "completed": summary.completed,
            "status": summary.state,
        }
    )


async def ingest_recorded_meeting(
    *,
    meeting_id: SessionId,
    identity: OperatorIdentity,
    twin_name: str | None,
    settings: Settings,
    no_correct: bool = False,
) -> None:
    succeeded = await run_recorded_ingest(
        IngestRequest(meeting_id, twin_name, no_correct),
        access=OperatorAccess(identity, settings, lambda: providers.open_persona_memory(settings)),
        ports=IngestPorts(stop_signals=_no_stop_signals, publish=_print_summary, report=emit_error),
    )
    if not succeeded:
        raise SystemExit(ExitCode.FAILURE)
