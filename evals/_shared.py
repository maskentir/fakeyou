import gc
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from langchain_core.language_models.chat_models import BaseChatModel

from fakeyou.config.settings import Settings

CREDENTIAL_MARKERS = ("key", "token", "secret", "password", "passwd", "auth", "credential")


@dataclass(frozen=True, kw_only=True, slots=True)
class ArtifactMeta:
    settings: Settings
    fixture: dict[str, Any]
    fixture_path: Path
    source_commit: str
    mode: str
    model_name: str


@dataclass(frozen=True, kw_only=True, slots=True)
class ReplayOptions:
    settings: Settings | None = None
    fallback: BaseChatModel | None = None
    fixture_path: Path | None = None


@contextmanager
def suspended_gc() -> Iterator[None]:
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def sanitize_url(value: str) -> str:
    if "://" not in value:
        return value
    try:
        parts = urlsplit(value)
        if not parts.scheme or not parts.netloc:
            return value
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        netloc = f"{host}{port}"
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except (TypeError, ValueError):
        return value


def _is_credential_key(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in CREDENTIAL_MARKERS)


def sanitize_extra_body(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _is_credential_key(key):
                cleaned[key] = "**********"
            else:
                cleaned[key] = sanitize_extra_body(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_extra_body(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_extra_body(item) for item in value)
    if isinstance(value, str) and "://" in value:
        return sanitize_url(value)
    return value


def sanitized_role(role: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(role)
    base_url = cleaned.get("base_url")
    if isinstance(base_url, str):
        cleaned["base_url"] = sanitize_url(base_url)
    for key, item in list(cleaned.items()):
        if key not in ("base_url", "extra_body") and isinstance(item, str) and "://" in item:
            cleaned[key] = sanitize_url(item)
    extra = cleaned.get("extra_body")
    if extra is not None:
        cleaned["extra_body"] = sanitize_extra_body(extra)
    return cleaned


def token_present(token: str, text: str) -> bool:
    return re.search(r"\b" + re.escape(token) + r"\b", text, re.IGNORECASE) is not None
