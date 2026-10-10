from fakeyou.observability.report import (
    VISUAL_BUDGET_MAX,
    VISUAL_BUDGET_P95,
    VISUAL_STABILITY_MIN,
    build_session_report,
)
from tests._report_support import row


def visual_captured(offset: float, visual_id: str, duration_ms: float) -> dict:
    return row(
        offset,
        "visual_captured",
        source="browser",
        visual_id=visual_id,
        kind="presentation",
        chars=1200,
        duration_ms=duration_ms,
    )


def visual_captioned(offset: float, visual_id: str, cost_usd: float, duration_ms: float) -> dict:
    return row(
        offset,
        "visual_captioned",
        source="llm",
        visual_id=visual_id,
        tokens=1100,
        cost_usd=cost_usd,
        duration_ms=duration_ms,
    )


def test_report_counts_visuals() -> None:
    rows = [
        visual_captured(0, "frame-a", 2500),
        visual_captured(10, "frame-b", 2500),
        visual_captioned(12, "frame-a", 0.0011, 9000),
    ]

    assert build_session_report(rows)["visual"] == {
        "frames": 2,
        "captioned": 1,
        "cost_usd": 0.0011,
        "p95_ms": 2500,
    }
    assert build_session_report([])["visual"] == {
        "frames": 0,
        "captioned": 0,
        "cost_usd": 0.0,
        "p95_ms": None,
    }


def test_gate_constants_consumed() -> None:
    rows: list = []
    for index in range(20):
        rows.append(visual_captured(index * 10, f"frame-{index}", 2000))
    for index in range(19):
        rows.append(visual_captioned(index * 10 + 2, f"frame-{index}", 0.0011, 9000))
    section = build_session_report(rows)["visual"]

    assert section["frames"] == 20
    assert section["captioned"] / section["frames"] >= VISUAL_STABILITY_MIN
    assert section["p95_ms"] / 1000 <= VISUAL_BUDGET_P95
    assert section["p95_ms"] / 1000 <= VISUAL_BUDGET_MAX

    slow = build_session_report([visual_captured(0, "frame-slow", 20000)])["visual"]

    assert slow["p95_ms"] / 1000 > VISUAL_BUDGET_P95
