import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fakeyou.config.settings import Settings

INVALID_ENDPOINT = "<invalid endpoint>"
REDACTED = "**********"
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_CREDENTIAL_TOKENS = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "key",
        "oauth",
        "passcode",
        "passphrase",
        "passwd",
        "password",
        "pwd",
        "secret",
        "token",
    }
)
_COMPACT_MARKERS = (
    "accesstoken",
    "apikey",
    "authkey",
    "authtoken",
    "bearer",
    "clientsecret",
    "cookie",
    "credential",
    "passcode",
    "passphrase",
    "password",
    "privatekey",
    "refreshtoken",
    "secretkey",
    "sharedsecret",
)
_ENDPOINT_TOKENS = frozenset({"dsn", "endpoint", "uri", "url"})


def safe_endpoint(endpoint: Any) -> str:
    if not isinstance(endpoint, str) or not endpoint.strip():
        return INVALID_ENDPOINT
    try:
        parsed = urlsplit(endpoint.strip())
        host = parsed.hostname
        if not host:
            return INVALID_ENDPOINT
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except (ValueError, UnicodeError):
        return INVALID_ENDPOINT


def _key_tokens(name: str) -> list[str]:
    split = _CAMEL_BOUNDARY.sub("_", name)
    return [token for token in re.split(r"[^a-z0-9]+", split.lower()) if token]


def is_credential_key(name: str) -> bool:
    tokens = _key_tokens(name)
    for token in tokens:
        forms = {token, token.rstrip("0123456789")}
        forms |= {form[:-1] for form in forms if form.endswith("s") and len(form) > 1}
        if forms & _CREDENTIAL_TOKENS:
            return True
    compact = "".join(tokens)
    return any(marker in compact for marker in _COMPACT_MARKERS)


def _is_endpoint_key(name: str) -> bool:
    return any(token in _ENDPOINT_TOKENS for token in _key_tokens(name))


def _redact_strings(value: Any) -> Any:
    if isinstance(value, str):
        return REDACTED
    if isinstance(value, dict):
        return {name: _redact_strings(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact_strings(item) for item in value]
    return value


def _sanitize_value(key: str, value: Any) -> Any:
    if key == "extra_body" or is_credential_key(key):
        return _redact_strings(value)
    if isinstance(value, dict):
        return {name: _sanitize_value(name, item) for name, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(key, item) for item in value]
    if isinstance(value, str):
        if value == REDACTED:
            return REDACTED
        if "://" in value or _is_endpoint_key(key):
            return safe_endpoint(value)
    return value


def sanitized_settings_dump(settings: Settings) -> dict[str, Any]:
    dumped = settings.model_dump(mode="json")
    return {name: _sanitize_value(name, item) for name, item in dumped.items()}
