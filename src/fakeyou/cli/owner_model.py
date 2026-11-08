import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx2
from pydantic import BaseModel, TypeAdapter

from fakeyou.catalog.errors import ErrorCode
from fakeyou.cli.client import ClientSettings, api_failure, owner_client
from fakeyou.cli.output import emit_json
from fakeyou.config.identifiers import SessionIdField, parse_session_id
from fakeyou.db.owner_model import Confidence, ItemDisclosure, OwnerModelKind


@dataclass(frozen=True, slots=True)
class OwnerModelItem:
    kind: OwnerModelKind
    topic: str
    statement: str
    confidence: Confidence
    disclosure: ItemDisclosure
    reason: str | None = None
    valid_at: datetime | None = None


_ITEM = TypeAdapter(OwnerModelItem)


def _body(item: OwnerModelItem) -> dict[str, object]:
    body: dict[str, object] = _ITEM.dump_python(item, mode="json")
    return body


class PersonaKnowledge(BaseModel):
    twin_id: SessionIdField
    verified_facts: tuple[str, ...] = ()
    goals: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OwnerModelTarget:
    settings: ClientSettings
    twin_id: str
    transport: httpx2.AsyncBaseTransport | None = None


async def show_owner_model(target: OwnerModelTarget, *, version: int | None) -> None:
    async with owner_client(target.settings, transport=target.transport) as client:
        emit_json(await client.owner_model(target.twin_id, version=version))


async def add_owner_model_item(target: OwnerModelTarget, item: OwnerModelItem) -> None:
    async with owner_client(target.settings, transport=target.transport) as client:
        emit_json(await client.add_owner_model_item(target.twin_id, _body(item)))


async def revise_owner_model_item(
    target: OwnerModelTarget, item_id: str, item: OwnerModelItem
) -> None:
    async with owner_client(target.settings, transport=target.transport) as client:
        emit_json(await client.revise_owner_model_item(target.twin_id, item_id, _body(item)))


async def retire_owner_model_item(target: OwnerModelTarget, item_id: str) -> None:
    async with owner_client(target.settings, transport=target.transport) as client:
        emit_json(await client.retire_owner_model_item(target.twin_id, item_id))


async def forget_owner_model_item(target: OwnerModelTarget, item_id: str) -> None:
    async with owner_client(target.settings, transport=target.transport) as client:
        await client.forget_owner_model_item(target.twin_id, item_id)
    emit_json({"item_id": item_id, "forgotten": True})


async def show_owner_model_history(target: OwnerModelTarget, item_id: str) -> None:
    async with owner_client(target.settings, transport=target.transport) as client:
        emit_json(await client.owner_model_history(target.twin_id, item_id))


async def show_owner_model_proposals(target: OwnerModelTarget) -> None:
    async with owner_client(target.settings, transport=target.transport) as client:
        emit_json(await client.owner_model_proposals(target.twin_id))


async def decide_owner_model_proposal(
    target: OwnerModelTarget, revision_id: str, *, approve: bool
) -> None:
    async with owner_client(target.settings, transport=target.transport) as client:
        emit_json(
            await client.decide_owner_model_proposal(target.twin_id, revision_id, approve=approve)
        )


async def import_persona_knowledge(
    settings: ClientSettings,
    persona_path: Path,
    *,
    twin_id: str | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    try:
        raw = await asyncio.to_thread(persona_path.read_bytes)
    except OSError as error:
        raise api_failure(ErrorCode.MEMORY_PERSONA_UNREADABLE, reason=error.strerror) from None
    knowledge = PersonaKnowledge.model_validate_json(raw)
    twin = parse_session_id(twin_id, field="--twin-id") if twin_id else knowledge.twin_id
    statements = {"facts": knowledge.verified_facts, "preferences": knowledge.goals}
    async with owner_client(settings, transport=transport) as client:
        emit_json(await client.import_owner_model(twin, statements))
