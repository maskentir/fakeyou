from dataclasses import asdict, dataclass

import httpx2

from fakeyou.cli.client import ClientSettings, owner_client
from fakeyou.cli.output import emit_json
from fakeyou.db.owner_model import Confidence, ItemDisclosure, OwnerModelKind


@dataclass(frozen=True, slots=True)
class OwnerModelItem:
    kind: OwnerModelKind
    topic: str
    statement: str
    confidence: Confidence
    disclosure: ItemDisclosure
    reason: str | None = None


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
        emit_json(await client.add_owner_model_item(target.twin_id, asdict(item)))


async def revise_owner_model_item(
    target: OwnerModelTarget, item_id: str, item: OwnerModelItem
) -> None:
    async with owner_client(target.settings, transport=target.transport) as client:
        emit_json(await client.revise_owner_model_item(target.twin_id, item_id, asdict(item)))


async def retire_owner_model_item(target: OwnerModelTarget, item_id: str) -> None:
    async with owner_client(target.settings, transport=target.transport) as client:
        emit_json(await client.retire_owner_model_item(target.twin_id, item_id))


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
