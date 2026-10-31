import logging
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import SecretStr

from fakeyou.api.overrides import overrides_from_headers
from fakeyou.api.sessions import SESSION_FILE_NAME
from fakeyou.catalog.config import PROVIDER_ROLES, ROLE_ATTRIBUTE_PATH, ROLE_HEADER_SUFFIX
from fakeyou.config.settings import Settings
from fakeyou.worker.keys import apply_role_overrides
from tests._sanitize_support import SYNTHETIC_MARKERS, assert_no_synthetic_markers
from tests.test_api_sessions import (
    OWNER_TOKEN,
    SESSION_PAYLOAD,
    ApiHarness,
    RecordingTransport,
)

MARKER_HEADERS: dict[str, str] = {
    f"X-Fakeyou-Key-{suffix}": f"{SYNTHETIC_MARKERS[index % len(SYNTHETIC_MARKERS)]}-{role}"
    for index, (role, suffix) in enumerate(ROLE_HEADER_SUFFIX.items())
}


def test_overrides_recognize_every_frozen_role_suffix() -> None:
    headers = {
        f"X-Fakeyou-Key-{suffix}": f"key-{role}" for role, suffix in ROLE_HEADER_SUFFIX.items()
    }
    found = overrides_from_headers(headers)
    assert set(found) == set(PROVIDER_ROLES)
    assert found["turn_judge"].get_secret_value() == "key-turn_judge"
    assert overrides_from_headers({"X-Fakeyou-Key-Unknown": "x"}) == {}


def test_overrides_match_header_names_case_insensitively() -> None:
    found = overrides_from_headers({"x-fakeyou-key-debrief": "debrief-key"})

    assert found["debrief"].get_secret_value() == "debrief-key"


def test_empty_header_values_are_not_overrides() -> None:
    assert overrides_from_headers({"X-Fakeyou-Key-Reply": ""}) == {}
    assert overrides_from_headers({"X-Fakeyou-Key-Reply": "   "}) == {}


def test_unknown_suffix_headers_are_ignored_without_recording() -> None:
    found = overrides_from_headers(
        {"X-Fakeyou-Key-Reply-Extra": "leak-1", "X-Fakeyou-Key-Unknown": "leak-2"}
    )

    assert found == {}


class FieldRecordingTransport(RecordingTransport):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[tuple[str, dict[str, str]]] = []
        self.overrides: dict[str, dict[str, str]] = {}

    async def store_overrides(
        self, meeting_id: str, overrides: Mapping[str, str], *, ttl_seconds: float
    ) -> None:
        del ttl_seconds
        self.overrides[meeting_id] = dict(overrides)

    async def take_overrides(self, meeting_id: str) -> dict[str, str]:
        return self.overrides.pop(meeting_id, {})


def _role_node(settings: Settings, role: str) -> Any:
    node: Any = settings
    for part in ROLE_ATTRIBUTE_PATH[role].split("."):
        node = getattr(node, part)
    return node


def _role_api_key(settings: Settings, role: str) -> str | None:
    node = _role_node(settings, role)
    if node is None:
        return None
    secret = node.api_key
    if secret is None:
        return None
    return str(secret.get_secret_value())


async def test_session_keys_never_leak_and_reach_only_the_worker_copy(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    harness = ApiHarness(settings)
    recorder = FieldRecordingTransport()
    harness.transport = recorder
    try:
        with caplog.at_level(logging.DEBUG):
            created = await harness.post(
                "/v1/sessions", json=SESSION_PAYLOAD, headers=MARKER_HEADERS
            )
        assert created.status_code == 201
        meeting_id = created.json()["meeting_id"]
        with caplog.at_level(logging.DEBUG):
            taken = await recorder.take_overrides(meeting_id)
            overrides = {role: SecretStr(value) for role, value in taken.items()}
            again = await recorder.take_overrides(meeting_id)
            retired = await harness.client.delete(
                f"/v1/internal/sessions/{meeting_id}/key-overrides",
                headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
            )
            with caplog.at_level(logging.DEBUG):
                joined = await harness.post(f"/v1/sessions/{meeting_id}/join")
                status = await harness.get(f"/v1/sessions/{meeting_id}")
                document = await harness.get("/openapi.json")

        assert set(overrides) == set(PROVIDER_ROLES)
        assert overrides["reply"].get_secret_value() == MARKER_HEADERS["X-Fakeyou-Key-Reply"]
        assert (
            overrides["turn_judge"].get_secret_value() == MARKER_HEADERS["X-Fakeyou-Key-Turn-Judge"]
        )
        assert again == {}
        assert retired.status_code == 404
        assert joined.status_code == 202
        assert status.status_code == 200

        session_file = settings.paths.record_dir / meeting_id / SESSION_FILE_NAME
        assert_no_synthetic_markers(session_file.read_text(encoding="utf-8"))
        assert_no_synthetic_markers(created.text)
        assert_no_synthetic_markers(joined.text)
        assert_no_synthetic_markers(status.text)
        assert_no_synthetic_markers(document.text)
        assert not [path for path in document.json()["paths"] if "/internal/" in path]
        assert_no_synthetic_markers(caplog.text)

        process_keys = {role: _role_api_key(settings, role) for role in PROVIDER_ROLES}
        session_settings = apply_role_overrides(settings, overrides)
        for role in PROVIDER_ROLES:
            header = f"X-Fakeyou-Key-{ROLE_HEADER_SUFFIX[role]}"
            if _role_node(session_settings, role) is None:
                assert _role_api_key(session_settings, role) is None
            else:
                assert _role_api_key(session_settings, role) == MARKER_HEADERS[header]
            assert _role_api_key(settings, role) == process_keys[role]
    finally:
        await harness.aclose()
