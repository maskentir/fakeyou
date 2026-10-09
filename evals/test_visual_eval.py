import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage
from PIL import Image

import fakeyou.debrief as debrief_module
from fakeyou.agent import create_visual_tools
from fakeyou.debrief_schema import DebriefDraft, DebriefItem, DebriefReport, EvidenceSource
from fakeyou.debrief_sources import load_record_snapshot, validate_draft
from fakeyou.report import VISUAL_BUDGET_MAX, VISUAL_BUDGET_P95, VISUAL_STABILITY_MIN, summarize
from fakeyou.settings import VisualSettings
from fakeyou.visual_caption import VisualCaptioner, frame_ref
from fakeyou.visual_hydrate import visual_clarification
from fakeyou.visual_store import VisualFrame, VisualStore, frames_in_window
from tests.fakes import RecordingJournal, ToolCallingFakeModel

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "evals" / "fixtures" / "visual-scenarios.json"
SCHEMA = "visual_eval_fixture/v1"
EXPECTED_IDS = (
    "answer_correct_visual",
    "clarify_ambiguous",
    "clarify_missing",
    "debrief_cites_visual",
    "budget_caps_hold",
    "static_slide_asked_late",
)
FRAME_PATTERN = re.compile(r"frame:[0-9a-f]{8}@\S+")
MS_PER_SECOND = 1000.0


def load_visual_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if fixture["schema_version"] != SCHEMA:
        raise ValueError("visual fixture schema mismatch")
    ids = [scenario["id"] for scenario in fixture["scenarios"]]
    if tuple(ids) != EXPECTED_IDS:
        raise ValueError("visual fixture scenario mismatch")
    return fixture


def scenario_by_id(fixture: dict[str, Any], wanted: str) -> dict[str, Any]:
    for scenario in fixture["scenarios"]:
        if scenario["id"] == wanted:
            return scenario
    raise ValueError(f"unknown visual scenario {wanted}")


def frame_by_id(fixture: dict[str, Any], wanted: str) -> dict[str, Any]:
    for row in fixture["frames"]:
        if row["frame_id"] == wanted:
            return row
    raise ValueError(f"unknown visual frame {wanted}")


def frames_from_rows(rows: list[dict[str, Any]]) -> list[VisualFrame]:
    return [
        VisualFrame(
            frame_id=row["frame_id"],
            captured_page_ms=row["captured_page_ms"],
            captured_at=row["captured_at"],
            sha256=row["sha256"],
            path="",
            thumb_path="",
            dhash=row["dhash"],
            visible_to_page_ms=row.get("visible_to_page_ms"),
            visible_to_at=row.get("visible_to_at"),
        )
        for row in rows
    ]


def assert_neutral_clarification(reply: str, forbidden: list[str]) -> None:
    assert reply.count("?") == 1
    assert FRAME_PATTERN.search(reply) is None
    lowered = reply.lower()
    for term in forbidden:
        assert term.lower() not in lowered


def shot_file(directory: Path, name: str) -> Path:
    path = directory / name
    Image.new("RGB", (64, 48), (20, 40, 80)).save(path, format="JPEG", quality=60)
    return path


def store_for(
    directory: Path, rows: list[dict[str, Any]], journal: RecordingJournal
) -> VisualStore:
    store = VisualStore(directory, journal=journal)
    for row in rows:
        shot = shot_file(directory, f"{row['frame_id']}.jpg")
        thumb = directory / f"{row['frame_id']}.thumb.jpg"
        thumb.write_bytes(b"thumb")
        store.append(
            VisualFrame(
                frame_id=row["frame_id"],
                captured_page_ms=row["captured_page_ms"],
                captured_at=row["captured_at"],
                sha256=row["sha256"],
                path=str(shot),
                thumb_path=str(thumb),
                dhash=row["dhash"],
            )
        )
    return store


def captioner_for(
    captions: list[str], settings: VisualSettings, journal: RecordingJournal
) -> VisualCaptioner:
    responses = [
        AIMessage(
            content=text,
            usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100},
        )
        for text in captions
    ]
    return VisualCaptioner(
        ToolCallingFakeModel(responses=responses), settings=settings, journal=journal
    )


def test_fixture_covers_six_anonymized_visual_scenarios() -> None:
    fixture = load_visual_fixture()

    assert fixture["schema_version"] == SCHEMA
    assert [scenario["id"] for scenario in fixture["scenarios"]] == list(EXPECTED_IDS)
    assert isinstance(fixture["tolerance_ms"], int)
    assert "meet.google.com" not in json.dumps(fixture)


async def test_answer_cites_frame_matching_utterance_window(tmp_path: Path) -> None:
    fixture = load_visual_fixture()
    scenario = scenario_by_id(fixture, "answer_correct_visual")
    tolerance_ms = fixture["tolerance_ms"]
    utterance = scenario["utterance"]
    journal = RecordingJournal("visual-eval")
    settings = VisualSettings(enabled=True)
    store = store_for(tmp_path / "answer", fixture["frames"], journal)
    candidates = frames_in_window(
        store.frames, utterance["from_ms"], utterance["to_ms"], tolerance_ms=tolerance_ms
    )

    assert [frame.frame_id for frame in candidates] == [scenario["expected_frame"]]
    expected = frame_by_id(fixture, scenario["expected_frame"])
    captioner = captioner_for([expected["caption"]], settings, journal)
    [describe] = create_visual_tools(store, captioner)
    reply = await describe.ainvoke({"frame_id": scenario["expected_frame"]})
    frame = next(item for item in store.frames if item.frame_id == scenario["expected_frame"])

    assert frame_ref(frame) == scenario["expected_ref"]
    assert scenario["expected_ref"] in reply
    assert expected["caption"] in reply
    assert (
        utterance["from_ms"] - tolerance_ms
        <= frame.captured_page_ms
        <= utterance["to_ms"] + tolerance_ms
    )


async def test_static_slide_asked_late_still_cites(tmp_path: Path) -> None:
    fixture = load_visual_fixture()
    scenario = scenario_by_id(fixture, "static_slide_asked_late")
    tolerance_ms = fixture["tolerance_ms"]
    utterance = scenario["utterance"]
    journal = RecordingJournal("visual-eval")
    settings = VisualSettings(enabled=True)
    store = store_for(tmp_path / "late", fixture["frames"], journal)
    candidates = frames_in_window(
        store.frames, utterance["from_ms"], utterance["to_ms"], tolerance_ms=tolerance_ms
    )

    assert [frame.frame_id for frame in candidates] == [scenario["expected_frame"]]
    expected = frame_by_id(fixture, scenario["expected_frame"])
    captioner = captioner_for([expected["caption"]], settings, journal)
    [describe] = create_visual_tools(store, captioner)
    reply = await describe.ainvoke({"frame_id": scenario["expected_frame"]})
    frame = next(item for item in store.frames if item.frame_id == scenario["expected_frame"])

    assert frame_ref(frame) == scenario["expected_ref"]
    assert scenario["expected_ref"] in reply
    assert expected["caption"] in reply
    assert frame.visible_to_page_ms is not None
    assert frame.captured_page_ms <= utterance["to_ms"] + tolerance_ms
    assert frame.visible_to_page_ms >= utterance["from_ms"] - tolerance_ms


def test_ambiguous_reference_yields_single_neutral_clarification() -> None:
    fixture = load_visual_fixture()
    scenario = scenario_by_id(fixture, "clarify_ambiguous")
    utterance = scenario["utterance"]
    frames = frames_from_rows(fixture["frames"])
    candidates = frames_in_window(
        frames, utterance["from_ms"], utterance["to_ms"], tolerance_ms=fixture["tolerance_ms"]
    )
    reply = visual_clarification(len(candidates))

    assert sorted(frame.frame_id for frame in candidates) == sorted(scenario["candidates"])
    assert len(candidates) > 1
    assert reply is not None
    assert_neutral_clarification(reply, scenario["forbidden_terms"])


def test_missing_visual_yields_neutral_clarification() -> None:
    fixture = load_visual_fixture()
    scenario = scenario_by_id(fixture, "clarify_missing")
    utterance = scenario["utterance"]
    frames = frames_from_rows(fixture["frames"])
    candidates = frames_in_window(
        frames, utterance["from_ms"], utterance["to_ms"], tolerance_ms=fixture["tolerance_ms"]
    )
    reply = visual_clarification(len(candidates))

    assert candidates == []
    assert reply is not None
    assert_neutral_clarification(reply, scenario["forbidden_terms"])


def test_debrief_cites_visual_frame(tmp_path: Path) -> None:
    fixture = load_visual_fixture()
    scenario = scenario_by_id(fixture, "debrief_cites_visual")
    visual = frame_by_id(fixture, scenario["visual_frame"])
    record_path = tmp_path / "visual-meeting" / "record.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    visual_row = {
        "kind": "visual",
        "at": visual["captured_at"],
        "visual_id": visual["frame_id"],
        "captured_at": visual["captured_at"],
        "captured_page_ms": visual["captured_page_ms"],
        "sha256": visual["sha256"],
        "caption": visual["caption"],
        "frame_ref": scenario["expected_ref"],
    }
    record_path.write_text(
        json.dumps(scenario["transcript"]) + "\n" + json.dumps(visual_row) + "\n",
        encoding="utf-8",
    )
    snapshot = load_record_snapshot(record_path)
    cited = DebriefItem.model_validate(
        {
            "kind": "decision_claim",
            "text": scenario["item_text"],
            "status": "stated",
            "evidence": [{"entry_id": scenario["expected_ref"], "quote": scenario["quote"]}],
        }
    )
    sources = {entry.id: entry for entry in snapshot.sources}

    assert scenario["expected_ref"] in sources
    assert sources[scenario["expected_ref"]].kind == "visual"
    validate_draft(DebriefDraft(items=(cited,), findings=()), sources=sources, brief=None)
    report = DebriefReport.model_validate(
        {
            "schema_version": "meeting_debrief/v1",
            "owner_id": "visual-eval-owner",
            "twin_id": "visual-eval-twin",
            "meeting_id": "visual-meeting",
            "record_sha256": snapshot.record_sha256,
            "context_sha256": None,
            "generated_at": datetime.now(UTC),
            "provider": "fake",
            "model": "fake-model",
            "source_count": len(snapshot.sources),
            "chunk_count": 1,
            "status": "completed",
            "review_status": "unreviewed",
            "items": [cited],
            "findings": [],
            "evidence_sources": [
                EvidenceSource(
                    id=entry.id,
                    text=entry.text,
                    kind=entry.kind,
                    participant_id=entry.participant_id,
                    at=entry.at,
                )
                for entry in snapshot.sources
            ],
            "source_limitations": ["Unknown attribution."],
        }
    )
    rendered = debrief_module.render_debrief(report)

    assert report.items[0].evidence[0].entry_id == scenario["expected_ref"]
    assert f"frame&#58;{visual['sha256'][:8]}" in rendered
    assert scenario["quote"] in rendered
    assert len(scenario["quote"]) <= 1000


async def test_budget_caps_hold_on_replay(tmp_path: Path) -> None:
    fixture = load_visual_fixture()
    scenario = scenario_by_id(fixture, "budget_caps_hold")
    journal = RecordingJournal("visual-eval")
    settings = VisualSettings(enabled=True)
    store = store_for(tmp_path / "budget", fixture["frames"], journal)
    captions = [frame_by_id(fixture, frame_id)["caption"] for frame_id in scenario["replay_frames"]]
    captioner = captioner_for(captions, settings, journal)
    frames = [item for item in store.frames if item.frame_id in scenario["replay_frames"]]
    results = [await captioner.caption_frame(item) for item in frames]
    captioned = journal.events("visual_captioned")
    durations = [row["duration_ms"] for row in captioned]
    stats = summarize(durations)
    resolved = len([item for item in results if item is not None])

    assert len(captioned) == len(scenario["replay_frames"])
    assert len(captioned) <= settings.max_calls
    assert captioner.spent_usd <= settings.budget_usd
    assert stats["p95"] / MS_PER_SECOND <= VISUAL_BUDGET_P95
    assert stats["max"] / MS_PER_SECOND <= VISUAL_BUDGET_MAX
    assert resolved / len(results) >= VISUAL_STABILITY_MIN
