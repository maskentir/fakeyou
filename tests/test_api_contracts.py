import json
from pathlib import Path
from typing import Any

import httpx2

from fakeyou.api.app import create_app
from fakeyou.config.settings import Settings
from tests._sanitize_support import SYNTHETIC_MARKERS, assert_no_synthetic_markers

SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "openapi-v1.json"


def _client(settings: Settings) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=create_app(settings)),
        base_url="http://testserver",
    )


def _shape(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "openapi": document["openapi"],
        "paths": {
            path: sorted(operations) for path, operations in sorted(document["paths"].items())
        },
        "schemas": sorted(document["components"]["schemas"]),
    }


async def test_generated_openapi_matches_the_frozen_shape(settings: Settings) -> None:
    async with _client(settings) as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    document = response.json()
    assert document["openapi"].startswith("3.1")
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert _shape(document) == frozen
    assert all(path.startswith("/v1") for path in document["paths"])


async def test_error_envelope_is_single_shaped_and_marker_free(settings: Settings) -> None:
    marker = SYNTHETIC_MARKERS[0]
    async with _client(settings) as client:
        response = await client.post(
            "/v1/sessions",
            json={"unexpected": marker},
            headers={"Authorization": f"Bearer {marker}"},
        )

    assert response.status_code in (401, 422)
    payload = response.json()
    assert set(payload) == {"error"}
    assert set(payload["error"]) <= {"code", "message", "details"}
    assert payload["error"]["code"]
    assert_no_synthetic_markers(response.text)
