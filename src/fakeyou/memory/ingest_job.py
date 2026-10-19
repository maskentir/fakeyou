from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionIdField, parse_session_id
from fakeyou.memory.ingest_service import (
    IngestPorts,
    IngestRequest,
    run_recorded_correction,
    run_recorded_ingest,
)
from fakeyou.memory.ingest_status import IngestSummary
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.persistence.ingestion_status import read_status
from fakeyou.persistence.layout import INGESTION_FILE_NAME

IngestionJobKind = Literal["ingest", "correct"]


@dataclass(frozen=True, slots=True)
class IngestionJob:
    meeting_id: str
    kind: IngestionJobKind


class _IngestionJobFields(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    v: Literal["1"]
    type: Literal["ingestion_job"]
    at: AwareDatetime
    meeting_id: SessionIdField
    kind: IngestionJobKind


def parse_ingestion_job(fields: Mapping[str, str]) -> IngestionJob:
    try:
        parsed = _IngestionJobFields.model_validate(dict(fields))
    except ValidationError as error:
        names = sorted({str(item["loc"][0]) for item in error.errors() if item["loc"]})
        raise ValueError(f"invalid ingestion job field: {', '.join(names)}") from None
    return IngestionJob(meeting_id=parsed.meeting_id, kind=parsed.kind)


def _corrupt_status_summary(meeting_id: str) -> IngestSummary:
    return IngestSummary(
        meeting_id=meeting_id,
        state="failed",
        windows=0,
        resumed_from=0,
        ingested=0,
        facts=0,
        completed=False,
        succeeded=False,
        stored=False,
    )


async def handle_ingestion_job(
    job: IngestionJob,
    *,
    access: OperatorAccess,
    ports: IngestPorts,
    ack: Callable[[], Awaitable[None]],
) -> IngestSummary:
    published: list[IngestSummary] = []

    def capture(summary: IngestSummary) -> None:
        published.append(summary)
        ports.publish(summary)

    service = run_recorded_ingest if job.kind == "ingest" else run_recorded_correction
    try:
        await service(
            IngestRequest(parse_session_id(job.meeting_id, field="meeting_id"), None),
            access=access,
            ports=replace(ports, publish=capture),
        )
    except FakeYouError as error:
        if error.code is not ErrorCode.INGESTION_STATUS_CORRUPT:
            raise
        ports.report(error)
        summary = _corrupt_status_summary(job.meeting_id)
        ports.publish(summary)
        return summary
    if not published:
        raise RuntimeError("ingestion job published no summary")
    if len(published) > 1:
        raise RuntimeError("ingestion job published more than one summary")
    [summary] = published
    if summary.stored:
        await ack()
    return summary


def pending_ingestion_meetings(record_root: Path) -> list[str]:
    pending: list[str] = []
    for status_file in sorted(record_root.glob(f"*/{INGESTION_FILE_NAME}")):
        status = read_status(status_file)
        if status is not None and status.status == "pending":
            pending.append(status_file.parent.name)
    return sorted(pending)
