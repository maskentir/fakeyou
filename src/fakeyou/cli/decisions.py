from pathlib import Path

import httpx2
from pydantic import ValidationError

from fakeyou.catalog.errors import ErrorCode
from fakeyou.cli.client import ClientSettings, api_failure, owner_client
from fakeyou.cli.output import emit_json
from fakeyou.debrief.lineage import LineageImport


def _curated_document(document_path: Path) -> LineageImport:
    try:
        return LineageImport.model_validate_json(document_path.read_bytes())
    except OSError as error:
        raise api_failure(
            ErrorCode.INPUT_INVALID, reason=f"cannot read {document_path}: {error.strerror}"
        ) from None
    except ValidationError as error:
        raise api_failure(
            ErrorCode.INPUT_INVALID,
            reason=f"{document_path} is not a decision_lineage/v1 import: "
            f"{error.error_count()} problem(s) at "
            + ", ".join(".".join(map(str, item["loc"])) for item in error.errors()),
        ) from None


async def import_decision_lineage(
    settings: ClientSettings,
    document_path: Path,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    document = _curated_document(document_path)
    async with owner_client(settings, transport=transport) as client:
        emit_json(
            await client.import_decision_lineage(
                document.meeting_id, document.model_dump(mode="json")
            )
        )


async def show_decision_lineage(
    settings: ClientSettings,
    record_id: str,
    *,
    cursor: str | None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    async with owner_client(settings, transport=transport) as client:
        emit_json(await client.decision_lineage(record_id, cursor=cursor))
