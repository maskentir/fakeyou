import asyncio
import ipaddress
import ssl
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urlparse, urlsplit, urlunsplit

from fakeyou.provider_faults import trim_detail


@dataclass(frozen=True, slots=True)
class ProbeResult:
    host: str
    ok: bool
    latency_ms: float
    detail: str


def provider_probe_targets(
    *, tts_base_url: str, stt_ws_url: str, agent_base_url: str
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(raw for raw in (tts_base_url, stt_ws_url, agent_base_url) if raw.strip())
    )


def provider_probe_hosts(
    *, tts_base_url: str, stt_ws_url: str, agent_base_url: str
) -> tuple[str, ...]:
    hosts: list[str] = []
    for raw in provider_probe_targets(
        tts_base_url=tts_base_url, stt_ws_url=stt_ws_url, agent_base_url=agent_base_url
    ):
        try:
            parsed = urlparse(raw)
            name = parsed.hostname
        except ValueError:
            continue
        if name and name not in hosts:
            hosts.append(name)
    return tuple(hosts)


def _endpoint_parts(endpoint: str) -> tuple[str, int, bool] | None:
    raw = endpoint if "://" in endpoint else f"https://{endpoint}"
    try:
        parsed = urlparse(raw)
        host = parsed.hostname
        if not host:
            return None
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "ws", "wss"}:
            return None
        secure = scheme in {"https", "wss"}
        return host, parsed.port or (443 if secure else 80), secure
    except (ValueError, UnicodeError):
        return None


async def probe_endpoint(
    endpoint: str, *, timeout_seconds: float | None = 5.0, ssl_context: ssl.SSLContext | None = None
) -> ProbeResult:
    started = time.monotonic()
    parts = _endpoint_parts(endpoint)
    if parts is None:
        return ProbeResult(_safe_endpoint(endpoint), False, 0.0, "invalid endpoint")
    host, port, secure = parts
    try:
        context = ssl_context or ssl.create_default_context() if secure else None
        server_name = host if secure and _is_dns_name(host) else None
        operation = _connect_and_close(host, port, context, server_name)
        if timeout_seconds is None:
            await operation
        else:
            async with asyncio.timeout(timeout_seconds):
                await operation
    except asyncio.CancelledError:
        raise
    except BaseException as error:
        detail = trim_detail(f"{type(error).__name__}: {error}") or type(error).__name__
        return ProbeResult(_safe_endpoint(endpoint), False, _elapsed(started), detail)
    detail = "tls handshake ok" if secure else "tcp connect ok"
    return ProbeResult(_safe_endpoint(endpoint), True, _elapsed(started), detail)


async def probe_host(
    host: str,
    *,
    timeout_seconds: float | None = 5.0,
    port: int = 443,
    ssl_context: ssl.SSLContext | None = None,
) -> ProbeResult:
    endpoint = f"https://{host}:{port}"
    result = await probe_endpoint(
        endpoint, timeout_seconds=timeout_seconds, ssl_context=ssl_context
    )
    return ProbeResult(host, result.ok, result.latency_ms, result.detail)


async def _connect_and_close(
    host: str, port: int, context: ssl.SSLContext | None, server_name: str | None
) -> None:
    reader, writer = await asyncio.open_connection(
        host, port, ssl=context, server_hostname=server_name
    )
    del reader
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()


def _is_dns_name(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _elapsed(started: float) -> float:
    return (time.monotonic() - started) * 1000


def _trim(detail: str, limit: int = 300) -> str:
    return (trim_detail(detail) or "")[:limit]


def _safe_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str):
        return "<invalid endpoint>"
    try:
        raw = endpoint if "://" in endpoint else f"https://{endpoint}"
        parsed = urlsplit(raw)
        if parsed.hostname is None:
            return trim_detail(endpoint) or "<invalid endpoint>"
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        safe = urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        return trim_detail(safe) or "<invalid endpoint>"
    except (ValueError, UnicodeError):
        return trim_detail(endpoint) or "<invalid endpoint>"


async def run_preflight(
    hosts: tuple[str, ...] | list[str],
    *,
    budget_seconds: float = 5.0,
    probe: Callable[[str], Awaitable[ProbeResult]] | None = None,
) -> list[ProbeResult]:
    endpoints = tuple(hosts)
    check = probe or _default_probe

    async def invoke(endpoint: str) -> ProbeResult:
        return await check(endpoint)

    semaphore = asyncio.Semaphore(5)

    async def bounded(endpoint: str) -> ProbeResult:
        async with semaphore:
            return await invoke(endpoint)

    tasks: list[asyncio.Task[ProbeResult]] = [
        asyncio.create_task(bounded(endpoint)) for endpoint in endpoints
    ]
    try:
        try:
            outcomes = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), budget_seconds
            )
        except TimeoutError:
            for task in tasks:
                task.cancel()
            outcomes = list(await asyncio.gather(*tasks, return_exceptions=True))
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    results: list[ProbeResult] = []
    for endpoint, outcome in zip(endpoints, outcomes, strict=True):
        if isinstance(outcome, ProbeResult):
            results.append(outcome)
        elif isinstance(outcome, asyncio.CancelledError):
            results.append(ProbeResult(endpoint, False, budget_seconds * 1000, "budget exceeded"))
        elif isinstance(outcome, BaseException):
            detail = _trim(f"{type(outcome).__name__}: {outcome}")
            results.append(ProbeResult(endpoint, False, 0.0, detail))
        else:
            results.append(ProbeResult(endpoint, False, 0.0, "invalid probe result"))
    return results


async def _default_probe(endpoint: str) -> ProbeResult:
    return await probe_endpoint(endpoint, timeout_seconds=None)
