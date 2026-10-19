import shutil

from fakeyou.catalog.errors import ErrorCode, FakeYouError


def _which(name: str) -> str | None:
    return shutil.which(name)


def resolve_node_binary() -> str:
    found = _which("node")
    if found is None:
        raise FakeYouError(
            ErrorCode.BROWSER_NODE_MISSING, reason="node executable not found on PATH"
        )
    return found


def resolve_docker_binary() -> str:
    found = _which("docker")
    if found is None:
        raise FakeYouError(
            ErrorCode.DOCKER_UNAVAILABLE, reason="docker executable not found on PATH"
        )
    return found
