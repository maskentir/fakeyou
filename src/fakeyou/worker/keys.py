from collections.abc import Mapping
from typing import Protocol, cast

from pydantic import BaseModel, SecretStr

from fakeyou.catalog.config import ROLE_ATTRIBUTE_PATH
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings


def _with_role_api_key(node: BaseModel, parts: list[str], key: SecretStr) -> BaseModel:
    head, *tail = parts
    target = getattr(node, head)
    if target is None:
        return node
    replaced: BaseModel = (
        _with_role_api_key(target, tail, key)
        if tail
        else target.model_copy(update={"api_key": key})
    )
    return node.model_copy(update={head: replaced})


def apply_role_overrides(settings: Settings, overrides: Mapping[str, SecretStr]) -> Settings:
    updated = settings
    for role, key in overrides.items():
        path = ROLE_ATTRIBUTE_PATH.get(role)
        if path is None:
            continue
        updated = cast(Settings, _with_role_api_key(updated, path.split("."), key))
    return updated


class OverrideSource(Protocol):
    async def take_overrides(self, scope: str) -> dict[str, str]: ...


async def job_settings(
    source: OverrideSource, base: Settings, scope: str, *, required: bool
) -> Settings:
    staged = await source.take_overrides(scope)
    if not staged and required:
        raise FakeYouError(ErrorCode.WORKER_JOB_KEYS_MISSING, scope=scope)
    return apply_role_overrides(base, {role: SecretStr(value) for role, value in staged.items()})


class KeyedSnapshot(Protocol):
    @property
    def keys_supplied(self) -> bool: ...


class SnapshotLoader(Protocol):
    def load(self, meeting_id: str) -> KeyedSnapshot | None: ...


async def meeting_job_settings(
    source: OverrideSource, store: SnapshotLoader, base: Settings, meeting_id: str
) -> Settings:
    snapshot = store.load(meeting_id)
    required = snapshot is not None and snapshot.keys_supplied
    return await job_settings(source, base, meeting_id, required=required)
