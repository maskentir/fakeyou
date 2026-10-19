from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fastapi import Request
from pydantic import SecretStr

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings

if TYPE_CHECKING:
    from fakeyou.api.ratelimit import RateLimiter

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Principal:
    kind: Literal["owner", "admin"]
    owner_id: str | None


def _digest(token: SecretStr | None) -> str | None:
    if token is None:
        return None
    raw = token.get_secret_value()
    if not raw.strip():
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _presented_token(authorization: str | None) -> str:
    if authorization is None:
        raise FakeYouError(ErrorCode.API_UNAUTHORIZED)
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme != "Bearer" or not token:
        raise FakeYouError(ErrorCode.API_UNAUTHORIZED)
    return token


class TokenStore:
    def __init__(self, *, owner_hash: str | None, admin_hash: str | None, owner_id: str) -> None:
        self._owner_hash = owner_hash
        self._admin_hash = admin_hash
        self._owner_id = owner_id

    @classmethod
    def from_settings(cls, settings: Settings) -> TokenStore:
        owner_hash = _digest(settings.api.owner_token)
        admin_hash = _digest(settings.api.admin_token)
        if owner_hash is None:
            logger.warning("api.owner_token is not configured; owner routes reject every request")
        if admin_hash is None:
            logger.warning("api.admin_token is not configured; admin routes reject every request")
        return cls(
            owner_hash=owner_hash,
            admin_hash=admin_hash,
            owner_id=str(settings.identity.owner_id),
        )

    def authenticate(self, authorization: str | None) -> Principal:
        presented = _presented_token(authorization)
        digest = hashlib.sha256(presented.encode("utf-8")).hexdigest()
        if self._owner_hash is not None and hmac.compare_digest(digest, self._owner_hash):
            return Principal(kind="owner", owner_id=self._owner_id)
        if self._admin_hash is not None and hmac.compare_digest(digest, self._admin_hash):
            return Principal(kind="admin", owner_id=None)
        raise FakeYouError(ErrorCode.API_UNAUTHORIZED)


def require_owned(principal: Principal, owner_id: str) -> None:
    if principal.owner_id != owner_id:
        raise FakeYouError(ErrorCode.API_NOT_FOUND)


def _guarded_principal(request: Request, kind: Literal["owner", "admin"]) -> Principal:
    store: TokenStore = request.app.state.token_store
    limiter: RateLimiter = request.app.state.rate_limiter
    principal = store.authenticate(request.headers.get("authorization"))
    if principal.kind != kind:
        raise FakeYouError(ErrorCode.API_FORBIDDEN)
    limiter.check(principal, at=time.monotonic())
    return principal


def require_owner(request: Request) -> Principal:
    return _guarded_principal(request, "owner")


def require_admin(request: Request) -> Principal:
    return _guarded_principal(request, "admin")
