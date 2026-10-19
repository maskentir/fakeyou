import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
REDIS_DIGEST = "redis@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    if shutil.which("docker") is None:
        raise RuntimeError("docker.unavailable: the docker binary is required to resolve compose")
    environment = dict(os.environ)
    environment.setdefault("POSTGRES_PASSWORD", "compose-parity-dummy")
    environment.setdefault("FAKEYOU_MEMORY__STORE__PASSWORD", "compose-parity-dummy")
    operators_env = (ROOT / ".env.example").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as scratch:
        project = Path(scratch)
        (project / "compose.yaml").write_bytes((ROOT / "compose.yaml").read_bytes())
        (project / ".env").write_text(operators_env, encoding="utf-8")
        result = subprocess.run(
            ["docker", "compose", "config", "--format", "json"],
            cwd=project,
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )
    if result.returncode != 0:
        raise RuntimeError(f"docker compose config failed: {result.stderr.strip()}")
    document: dict[str, Any] = json.loads(result.stdout)
    return document


def _seconds(duration: str) -> int:
    minutes = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s)?", duration)
    assert minutes is not None, duration
    return int(minutes.group(1) or 0) * 60 + int(minutes.group(2) or 0)


def test_api_redis_worker_services_are_loopback_and_long_lived(compose: dict[str, Any]) -> None:
    services = compose["services"]

    assert {
        "api",
        "redis",
        "worker-sessions",
        "worker-ingestion",
        "worker-debrief",
        "worker-reembed",
    } <= set(services)
    assert (
        services["redis"]["image"].endswith(REDIS_DIGEST)
        or "@sha256:" in services["redis"]["image"]
    )
    assert services["api"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert "docker.sock" not in json.dumps(services["api"])
    for name, role in (
        ("worker-sessions", "sessions"),
        ("worker-ingestion", "ingestion"),
        ("worker-debrief", "debrief"),
        ("worker-reembed", "reembed"),
    ):
        assert services[name]["command"][-3:] == ["-m", "fakeyou.worker", role]
    for name in (
        "worker-sessions",
        "worker-ingestion",
        "worker-debrief",
        "worker-reembed",
    ):
        assert _seconds(services[name]["stop_grace_period"]) == 136


def test_no_service_publishes_a_non_loopback_port(compose: dict[str, Any]) -> None:
    for name, service in compose["services"].items():
        for port in service.get("ports", []):
            assert port["host_ip"] == "127.0.0.1", name


def test_redis_is_a_transport_with_no_persistent_volume(compose: dict[str, Any]) -> None:
    redis = compose["services"]["redis"]

    assert redis["command"][:1] == ["redis-server"]
    assert "--appendonly" in redis["command"]
    assert redis["command"][redis["command"].index("--appendonly") + 1] == "no"
    assert not redis.get("volumes")
    assert redis["healthcheck"]["test"][-1].endswith("ping") or "ping" in json.dumps(
        redis["healthcheck"]
    )


def test_api_and_worker_reach_redis_by_service_name(compose: dict[str, Any]) -> None:
    for name in (
        "api",
        "worker-sessions",
        "worker-ingestion",
        "worker-debrief",
        "worker-reembed",
    ):
        environment = compose["services"][name]["environment"]
        assert environment["FAKEYOU_TRANSPORT__REDIS__URL"] == "redis://redis:6379/0", name


def test_api_binds_all_interfaces_inside_the_container(compose: dict[str, Any]) -> None:
    assert compose["services"]["api"]["environment"]["FAKEYOU_API__HOST"] == "0.0.0.0"


def test_api_and_worker_share_the_runtime_directory(compose: dict[str, Any]) -> None:
    def runtime_mounts(name: str) -> set[str]:
        return {
            volume["target"]
            for volume in compose["services"][name].get("volumes", [])
            if volume["target"] == "/workspace/.runtime"
        }

    assert runtime_mounts("api") == runtime_mounts("worker-sessions") == {"/workspace/.runtime"}
    for name in ("worker-ingestion", "worker-debrief", "worker-reembed"):
        assert runtime_mounts(name) == {"/workspace/.runtime"}


def test_api_is_not_granted_capabilities_and_is_read_only(compose: dict[str, Any]) -> None:
    api = compose["services"]["api"]

    assert api["read_only"] is True
    assert api["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in api["security_opt"]


WORKER_ROLE_SERVICES = (
    "worker-sessions",
    "worker-ingestion",
    "worker-debrief",
    "worker-reembed",
)


def test_every_worker_role_service_restarts_on_its_own(compose: dict[str, Any]) -> None:
    services = compose["services"]

    assert all(services[name]["restart"] == "unless-stopped" for name in WORKER_ROLE_SERVICES)


def test_every_worker_role_service_has_its_own_resource_knob() -> None:
    source = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    for name in WORKER_ROLE_SERVICES:
        knob = name.upper().replace("-", "_")
        assert f"mem_limit: ${{FAKEYOU_COMPOSE_{knob}_MEM_LIMIT:-" in source
        assert f"cpus: ${{FAKEYOU_COMPOSE_{knob}_CPUS:-" in source


def test_the_session_role_keeps_the_largest_share_of_memory(compose: dict[str, Any]) -> None:
    services = compose["services"]
    limits = {name: services[name]["mem_limit"] for name in WORKER_ROLE_SERVICES}

    assert limits["worker-sessions"] == max(limits.values())


def test_worker_services_reach_neo4j_by_service_name(compose: dict[str, Any]) -> None:
    services = compose["services"]
    environment = services["worker-sessions"]["environment"]

    assert environment["FAKEYOU_MEMORY__STORE__URI"].startswith("bolt://neo4j:7687")
    assert "localhost" not in environment["FAKEYOU_MEMORY__STORE__URI"]


def test_no_worker_service_points_a_service_dependency_at_localhost(
    compose: dict[str, Any],
) -> None:
    services = compose["services"]
    local = [
        (name, key)
        for name in WORKER_ROLE_SERVICES
        for key, value in services[name]["environment"].items()
        if isinstance(value, str)
        and ("localhost" in value or "127.0.0.1" in value)
        and key.startswith("FAKEYOU_")
        and key.endswith(("__URI", "__URL", "__BASE_URL"))
    ]

    assert local == []


def test_api_reaches_neo4j_and_the_embedder_by_service_name(compose: dict[str, Any]) -> None:
    environment = compose["services"]["api"]["environment"]

    assert environment["FAKEYOU_MEMORY__STORE__URI"].startswith("bolt://neo4j:7687")
    assert "localhost" not in environment["FAKEYOU_MEMORY__STORE__URI"]
    assert "embeddings" in environment["FAKEYOU_MEMORY__EMBEDDER__BASE_URL"]


def test_api_waits_for_neo4j_before_accepting_traffic(compose: dict[str, Any]) -> None:
    assert "neo4j" in compose["services"]["api"]["depends_on"]
