import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pytest

POSTGRES_IMAGE = (
    "postgres:17.11-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24"
)
NEO4J_IMAGE = (
    "neo4j:5.26.31-community@sha256:"
    "c7d25c0eeebfe125718d58b72ed5d663c85d0479047733845eb2237c67ce8069"
)
REDIS_IMAGE = (
    "redis:7.4-alpine@sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499"
)


@dataclass(frozen=True, slots=True)
class ServiceEndpoint:
    host: str
    port: int


def _run_docker(args: list[str], *, description: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.fail(f"{description} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def require_docker() -> None:
    if shutil.which("docker") is None:
        pytest.fail("docker binary is required for the real-service harness")
    _run_docker(["info", "--format", "{{.ServerVersion}}"], description="docker daemon probe")


def start_service(image: str, *, name: str, env: Mapping[str, str], container_port: int) -> str:
    args = [
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--label",
        "fakeyou-harness=1",
        "-p",
        f"127.0.0.1::{container_port}",
    ]
    for key, value in sorted(env.items()):
        args.extend(["-e", f"{key}={value}"])
    args.append(image)
    return _run_docker(args, description=f"start {name}")


def service_endpoint(container: str, container_port: int) -> ServiceEndpoint:
    output = _run_docker(
        ["port", container, f"{container_port}/tcp"], description=f"port for {container}"
    )
    host, _, port = output.splitlines()[-1].rpartition(":")
    return ServiceEndpoint(host=host, port=int(port))


def wait_ready(
    check: Callable[[], bool], *, description: str, timeout_seconds: float = 120.0
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.5)
    pytest.fail(f"{description} did not become ready within {timeout_seconds} seconds")


def stop_service(container: str) -> None:
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
