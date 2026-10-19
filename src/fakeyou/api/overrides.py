from collections.abc import Mapping
from typing import Protocol

from pydantic import SecretStr

from fakeyou.catalog.config import ROLE_HEADER_SUFFIX
from fakeyou.transport.streams import OVERRIDE_TTL_SECONDS

HEADER_PREFIX = "X-Fakeyou-Key-"


def overrides_from_headers(headers: Mapping[str, str]) -> dict[str, SecretStr]:
    known = {
        f"{HEADER_PREFIX}{suffix}".lower(): role for role, suffix in ROLE_HEADER_SUFFIX.items()
    }
    overrides: dict[str, SecretStr] = {}
    for name, value in headers.items():
        role = known.get(name.lower())
        if role is None or not value.strip():
            continue
        overrides[role] = SecretStr(value)
    return overrides


class OverrideStaging(Protocol):
    async def store_overrides(
        self, scope: str, overrides: Mapping[str, str], *, ttl_seconds: float
    ) -> None: ...


async def stage_overrides(store: OverrideStaging, scope: str, headers: Mapping[str, str]) -> None:
    overrides = overrides_from_headers(headers)
    if not overrides:
        return
    values = {role: secret.get_secret_value() for role, secret in overrides.items()}
    await store.store_overrides(scope, values, ttl_seconds=OVERRIDE_TTL_SECONDS)
