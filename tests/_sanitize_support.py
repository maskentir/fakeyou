import json
from typing import Any

SYNTHETIC_MARKERS = (
    "SYNTHETIC-VALUE-alpha-731",
    "SYNTHETIC-VALUE-bravo-731",
    "SYNTHETIC-VALUE-charlie-731",
)


def assert_no_synthetic_markers(payload: object) -> None:
    encoded = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    leaked = [marker for marker in SYNTHETIC_MARKERS if marker in encoded]
    assert leaked == []


def marked_role() -> dict[str, Any]:
    alpha, bravo, charlie = SYNTHETIC_MARKERS
    return {
        "provider": "openai_compatible",
        "model": "marked-model",
        "base_url": f"https://user:{alpha}@synthetic.example/v1?material={alpha}",
        "api_key": alpha,
        "timeout_seconds": 5.0,
        "max_retries": 1,
        "reasoning_effort": None,
        "unlisted_field": bravo,
        "extra_body": {
            "bearer_material": bravo,
            "private_signing_material": charlie,
            "routing": {"choices": ["start"]},
        },
    }
