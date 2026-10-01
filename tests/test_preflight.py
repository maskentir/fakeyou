import json
import ssl
import time
from pathlib import Path

import pytest

from fakeyou.preflight import ProbeResult, probe_endpoint, run_preflight

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LOCAL_CERT = FIXTURES / "preflight-localhost.pem"
LOCAL_KEY = FIXTURES / "preflight-localhost-key.pem"


def _trusting_client_context() -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=str(LOCAL_CERT))
    context.check_hostname = False
    return context


async def _serve_tls_once() -> tuple:
    import asyncio

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(LOCAL_CERT), str(LOCAL_KEY))
    server = await asyncio.start_server(
        lambda reader, writer: None, "127.0.0.1", 0, ssl=server_context
    )
    return server


async def test_probe_endpoint_succeeds_against_local_tls_stub() -> None:
    server = await _serve_tls_once()
    port = server.sockets[0].getsockname()[1]
    try:
        endpoint = f"https://127.0.0.1:{port}"
        result = await probe_endpoint(
            endpoint, timeout_seconds=5.0, ssl_context=_trusting_client_context()
        )
    finally:
        server.close()
        await server.wait_closed()

    assert result.ok is True
    assert result.host == "<url>"
    assert result.latency_ms >= 0
    assert json.dumps(result.detail) != ""


async def test_probe_endpoint_reports_closed_port() -> None:
    result = await probe_endpoint("https://127.0.0.1:9", timeout_seconds=2.0)

    assert result.ok is False
    assert "Error" in result.detail or "error" in result.detail.lower()
    assert result.latency_ms >= 0


async def test_probe_endpoint_never_raises_on_garbage() -> None:
    result = await probe_endpoint("not a host !@#", timeout_seconds=2.0)

    assert result.ok is False
    assert result.detail != ""


async def test_preflight_waits_for_all_endpoints_after_one_succeeds() -> None:
    import asyncio

    async def fast_ok(host: str) -> ProbeResult:
        return ProbeResult(host, True, 5.0, "fast")

    async def slow_fail(host: str) -> ProbeResult:
        await asyncio.sleep(0.02)
        return ProbeResult(host, False, 20.0, "slow")

    async def routed(host: str) -> ProbeResult:
        if host == "fast.example":
            return await fast_ok(host)
        return await slow_fail(host)

    started = time.monotonic()
    results = await run_preflight(
        ("fast.example", "slow.example"), budget_seconds=5.0, probe=routed
    )
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert [result.host for result in results] == ["fast.example", "slow.example"]
    assert [result.ok for result in results] == [True, False]


async def test_all_fail_returns_without_success() -> None:
    async def always_fail(host: str) -> ProbeResult:
        return ProbeResult(host, False, 1.0, "down")

    started = time.monotonic()
    results = await run_preflight(("a.example", "b.example"), budget_seconds=5.0, probe=always_fail)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert len(results) == 2
    assert all(not result.ok for result in results)


async def test_preflight_probes_all_endpoints_and_preserves_input_order() -> None:
    seen: list[str] = []

    async def probe(endpoint: str) -> ProbeResult:
        import asyncio

        seen.append(endpoint)
        await asyncio.sleep(0.02 if endpoint == "slow" else 0)
        return ProbeResult(endpoint, endpoint == "fast", 1.0, endpoint)

    results = await run_preflight(("slow", "fast", "down"), budget_seconds=1.0, probe=probe)

    assert seen == ["slow", "fast", "down"]
    assert [result.host for result in results] == ["slow", "fast", "down"]
    assert [result.ok for result in results] == [False, True, False]


async def test_http_endpoint_uses_plain_transport_and_explicit_port() -> None:
    async def probe_endpoint(endpoint: str) -> ProbeResult:
        return ProbeResult(endpoint, True, 1.0, "ok")

    results = await run_preflight(("http://127.0.0.1:8080/health",), probe=probe_endpoint)

    assert results[0].host == "http://127.0.0.1:8080/health"


async def test_run_preflight_never_raises() -> None:
    async def broken(host: str) -> ProbeResult:
        raise RuntimeError("probe exploded")

    results = await run_preflight(("x.example",), budget_seconds=1.0, probe=broken)

    assert len(results) == 1
    assert results[0].ok is False


@pytest.mark.live
async def test_live_tls_to_openai_opt_in() -> None:
    result = await probe_endpoint("https://api.openai.com", timeout_seconds=10.0)

    assert result.ok is True


@pytest.mark.parametrize("scheme", ["bolt+ssc", "neo4j+ssc"])
async def test_self_signed_neo4j_scheme_accepts_local_certificate(scheme):
    from fakeyou.preflight import probe_endpoint

    server = await _serve_tls_once()
    port = server.sockets[0].getsockname()[1]
    try:
        result = await probe_endpoint(f"{scheme}://127.0.0.1:{port}")
    finally:
        server.close()
        await server.wait_closed()
    assert result.ok, result.detail
    assert result.detail == "tls handshake ok"


@pytest.mark.parametrize("scheme", ["bolt+s", "neo4j+s", "https", "wss"])
async def test_verified_scheme_rejects_untrusted_local_certificate(scheme):
    from fakeyou.preflight import probe_endpoint

    server = await _serve_tls_once()
    port = server.sockets[0].getsockname()[1]
    try:
        result = await probe_endpoint(f"{scheme}://127.0.0.1:{port}")
    finally:
        server.close()
        await server.wait_closed()
    assert not result.ok
    assert "CERTIFICATE_VERIFY_FAILED" in result.detail
