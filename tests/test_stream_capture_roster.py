import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from fakeyou.audio.stream_capture import (
    DrainPolling,
    load_capture_script,
    parse_roster_names,
)
from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.cli.join import TranscriptSinks
from fakeyou.identity.identity import SpeakerRegistry
from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.observability.journal import Journal
from tests._capture_support import _recording_registry
from tests.fakes import FakeClock, meet_page


async def test_capture_drain_loop_emits_level_heartbeat() -> None:
    import json

    from fakeyou.audio.stream_capture import CaptureDrainLoop
    from fakeyou.observability.journal import Journal

    payload = {
        "streams": [
            {
                "slot": 0,
                "seq": 0,
                "csrcs": [111],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
                "track": "abc123",
                "engine": "decode",
            },
            {
                "slot": 1,
                "seq": 0,
                "csrcs": [],
                "level": 0.0,
                "page_ms": 1000.0,
                "pcm_b64": "AAA=",
                "track": "def456",
                "engine": "mstp",
            },
        ],
        "roster": [],
        "dropped": 2,
    }
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    page = meet_page([], evaluates=[payload, payload, payload])
    registry, _ = _recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(
        page, registry, journal=journal, polling=DrainPolling(heartbeat_polls=3)
    )
    assert await loop.poll() is True
    assert await loop.poll() is True
    assert lines == []
    assert await loop.poll() is True
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "capture_polled"
    assert event["dropped"] == 2
    assert event["fails"] == 0
    assert event["slots"]["0"] == {
        "level": 0.4,
        "bytes": 4,
        "track": "abc123",
        "engine": "decode",
        "stalled": False,
        "rescued": 0,
        "decode_fresh": False,
    }
    assert event["slots"]["1"] == {
        "level": 0.0,
        "bytes": 2,
        "track": "def456",
        "engine": "mstp",
        "stalled": False,
        "rescued": 0,
        "decode_fresh": False,
    }


def test_parse_roster_excludes_twin_self() -> None:
    from fakeyou.audio.stream_capture import parse_roster

    payload = {
        "roster": [
            {"id": "devices/1", "name": "Host"},
            {"id": "devices/2", "name": "Guest"},
            {"id": "devices/3", "name": "Visitor"},
            {"id": "devices/9", "name": "Twin"},
        ]
    }
    assert parse_roster(payload, twin_name="Twin") == ("devices/1", "devices/2", "devices/3")
    assert parse_roster(payload) == ("devices/1", "devices/2", "devices/3", "devices/9")


async def test_capture_drain_binds_three_remote_streams_with_self_present() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.audio.voice_activity import SlotBinder

    payload = {
        "streams": [
            {
                "slot": slot,
                "seq": 0,
                "csrcs": [111 + slot],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
            for slot in range(3)
        ],
        "roster": [
            {"id": "devices/1", "name": "Host"},
            {"id": "devices/2", "name": "Guest"},
            {"id": "devices/3", "name": "Visitor"},
            {"id": "devices/9", "name": "Twin"},
        ],
        "dropped": 0,
    }
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = _recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    labels = []
    for slot in range(3):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 3


async def test_stream_callback_surfaces_refusal_reason_and_roster_size(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.audio.voice_activity import SlotBinder
    from fakeyou.cli.join import build_transcript_callback
    from fakeyou.observability.journal import Journal
    from fakeyou.persistence.record import MeetingRecord, meeting_record_path

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, now=lambda: now, echo=False)
    path = meeting_record_path(tmp_path, "meeting-9")
    record = MeetingRecord(path, now=lambda: now)
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None),
        binder=SlotBinder(),
        voice_activity=None,
        per_stream_enabled=True,
    )
    frame = TranscriptionFrame(
        "Hello from the call.", "", "2026-10-06T01:00:01+00:00", finalized=True
    )
    frame.transport_source = "slot-0"
    await callback(frame)
    record.close()
    journal.close()
    rows = [json.loads(line) for line in lines]
    rows = [row for row in rows if row["event"] == "transcript_finalized"]
    assert len(rows) == 1
    assert rows[0]["participant_id"] == "unknown"
    assert rows[0]["speaker_reason"] == "no_roster"
    assert rows[0]["speaker_slot"] == 0
    assert rows[0]["roster_size"] == 0


async def test_stream_callback_surfaces_bound_reason_and_roster_size(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.audio.voice_activity import SlotBinder
    from fakeyou.cli.join import build_transcript_callback
    from fakeyou.observability.journal import Journal
    from fakeyou.persistence.record import MeetingRecord, meeting_record_path

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, now=lambda: now, echo=False)
    path = meeting_record_path(tmp_path, "meeting-9")
    record = MeetingRecord(path, now=lambda: now)
    binder = SlotBinder()
    binder.update_roster(["host"])
    binder.observe_stream(0, [111])
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None),
        binder=binder,
        voice_activity=None,
        per_stream_enabled=True,
    )
    frame = TranscriptionFrame(
        "Welcome to the call.", "", "2026-10-06T01:00:01+00:00", finalized=True
    )
    frame.transport_source = "slot-0"
    await callback(frame)
    record.close()
    journal.close()
    rows = [json.loads(line) for line in lines]
    rows = [row for row in rows if row["event"] == "transcript_finalized"]
    assert len(rows) == 1
    assert rows[0]["participant_id"] == "speaker-a"
    assert rows[0]["speaker_reason"] == "stream_slot"
    assert rows[0]["speaker_slot"] == 0
    assert rows[0]["speaker_bound"] is True
    assert rows[0]["roster_size"] == 1


async def test_capture_drain_loop_emits_roster_diagnostics() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop
    from fakeyou.observability.journal import Journal

    payload = {
        "streams": [
            {
                "slot": 0,
                "seq": 0,
                "csrcs": [6666],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
        ],
        "roster": [
            {"id": "devices/1", "name": "Host"},
            {"id": "devices/2", "name": "Guest"},
            {"id": "devices/3", "name": "Visitor"},
            {"id": "devices/9", "name": "Twin"},
        ],
        "roster_tiles": 6,
        "dropped": 0,
    }
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    page = meet_page([], evaluates=[payload])
    registry, _ = _recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(
        page, registry, journal=journal, polling=DrainPolling(heartbeat_polls=1), twin_name="Twin"
    )
    assert await loop.poll() is True
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "capture_polled"
    assert event["roster_tiles"] == 6
    assert event["roster_pre_filter"] == 4
    assert event["roster_post_filter"] == 3
    blob = json.dumps(event)
    assert "Host" not in blob
    assert "Guest" not in blob
    assert "Visitor" not in blob
    assert "devices/1" not in blob


async def test_capture_drain_loop_roster_diagnostics_fall_back_without_tiles() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop
    from fakeyou.observability.journal import Journal

    steady = {
        "streams": [
            {
                "slot": 0,
                "seq": 0,
                "csrcs": [6666],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
        ],
        "roster": [
            {"id": "devices/1", "name": "Host"},
            {"id": "devices/2", "name": "Guest"},
        ],
        "dropped": 0,
    }
    broken = {
        "streams": [
            {"slot": 0, "seq": 0, "csrcs": [], "level": 0.0, "page_ms": 1000.0, "pcm_b64": "AAA="}
        ],
        "roster": "broken",
        "roster_tiles": "nope",
        "dropped": 0,
    }
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    page = meet_page([], evaluates=[steady, broken])
    registry, _ = _recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(
        page, registry, journal=journal, polling=DrainPolling(heartbeat_polls=1)
    )
    assert await loop.poll() is True
    assert await loop.poll() is True
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["roster_tiles"] == 2
    assert first["roster_pre_filter"] == 2
    assert first["roster_post_filter"] == 2
    second = json.loads(lines[1])
    assert second["roster_tiles"] == 0
    assert second["roster_pre_filter"] == 0
    assert second["roster_post_filter"] == 0


@pytest.mark.browser
async def test_stream_capture_drain_reports_roster_tile_count() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate(
            "() => {"
            " document.body.innerHTML = \"<div data-participant-id='a'>\""
            " + \"<span class='notranslate'>A</span></div>\""
            " + \"<div data-participant-id=''></div>\""
            " + \"<div data-participant-id='b'>\""
            " + \"<span class='notranslate'>B</span></div>\";"
            "}"
        )
        drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    assert drained["roster_tiles"] == 3
    assert len(drained["roster"]) == 2


def test_parse_roster_keeps_all_entries_when_twin_name_collides() -> None:
    from fakeyou.audio.stream_capture import parse_roster

    payload = {
        "roster": [{"id": f"devices/{index}", "name": "Twin"} for index in range(5)],
    }
    assert parse_roster(payload, twin_name="Twin") == tuple(
        f"devices/{index}" for index in range(5)
    )
    mixed = {
        "roster": [{"id": f"devices/{index}", "name": "Twin"} for index in range(4)]
        + [{"id": "devices/9", "name": "Host"}],
    }
    assert parse_roster(mixed, twin_name="Twin") == (
        tuple(f"devices/{index}" for index in range(4)) + ("devices/9",)
    )


def _collision_payload(names: list[str]) -> dict[str, Any]:
    return {
        "streams": [
            {
                "slot": slot,
                "seq": 0,
                "csrcs": [111 + slot],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
            for slot in range(4)
        ],
        "roster": [{"id": f"devices/{index}", "name": name} for index, name in enumerate(names)],
        "dropped": 0,
    }


async def test_capture_drain_binds_four_streams_when_twin_names_collide() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.audio.voice_activity import SlotBinder

    payload = _collision_payload(["Twin"] * 5)
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = _recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    assert binder.roster_size == 5
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 4
    for slot in range(4):
        repeat = binder.resolve(slot)
        assert not isinstance(repeat, str)
        assert repeat.label == labels[slot]


async def test_capture_drain_binds_speakers_with_lurker_present() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.audio.voice_activity import SlotBinder

    payload = _collision_payload(["Twin"] * 4 + ["Host"])
    roster = list(payload["roster"])
    roster.append({"id": "devices/lurker", "name": "Lurker"})
    payload["roster"] = roster
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = _recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    assert binder.roster_size == 6
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 4
    for slot in range(4):
        repeat = binder.resolve(slot)
        assert not isinstance(repeat, str)
        assert repeat.label == labels[slot]


async def test_capture_drain_binds_four_humans_with_unique_names() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.audio.voice_activity import SlotBinder

    payload = _collision_payload(["Host", "Guest", "Visitor", "Partner", "Twin"])
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = _recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    assert binder.roster_size == 4
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        assert outcome.device != "devices/4"
        labels.append(outcome.label)
    assert len(set(labels)) == 4


async def test_capture_drain_loop_reports_collision_roster_diagnostics() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop
    from fakeyou.observability.journal import Journal

    payload = {
        "streams": [
            {
                "slot": 0,
                "seq": 0,
                "csrcs": [6666],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
        ],
        "roster": [{"id": f"devices/{index}", "name": "Twin"} for index in range(5)],
        "roster_tiles": 5,
        "dropped": 0,
    }
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    page = meet_page([], evaluates=[payload])
    registry, _ = _recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(
        page, registry, journal=journal, polling=DrainPolling(heartbeat_polls=1), twin_name="Twin"
    )
    assert await loop.poll() is True
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "capture_polled"
    assert event["roster_tiles"] == 5
    assert event["roster_pre_filter"] == 5
    assert event["roster_post_filter"] == 5
    blob = json.dumps(event)
    assert "Twin" not in blob
    assert "devices/0" not in blob


async def test_stream_callback_journals_csrc_and_tenure_events(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.audio.voice_activity import SlotBinder
    from fakeyou.cli.join import build_transcript_callback
    from fakeyou.observability.journal import Journal
    from fakeyou.persistence.record import MeetingRecord, meeting_record_path

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, now=lambda: now, echo=False)
    path = meeting_record_path(tmp_path, "meeting-9")
    record = MeetingRecord(path, now=lambda: now)
    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    binder.observe_stream(0, [111])
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None),
        binder=binder,
        voice_activity=None,
        per_stream_enabled=True,
    )
    first = TranscriptionFrame(
        "Welcome to the call.", "", "2026-10-06T01:00:01+00:00", finalized=True
    )
    first.transport_source = "slot-0"
    await callback(first)
    binder.observe_stream(1, [111])
    second = TranscriptionFrame(
        "Still here on the call.", "", "2026-10-06T01:00:02+00:00", finalized=True
    )
    second.transport_source = "slot-1"
    await callback(second)
    record.close()
    journal.close()
    rows = [json.loads(line) for line in lines]
    rows = [row for row in rows if row["event"] == "transcript_finalized"]
    assert len(rows) == 2
    assert rows[0]["speaker_csrc"] == [111]
    assert rows[0]["tenure_events"] == [{"event": "tenure_minted", "label": "A", "slot": 0}]
    assert rows[1]["tenure_events"] == [
        {"event": "tenure_moved", "label": "A", "from_slot": 0, "to_slot": 1, "csrcs": [111]}
    ]


async def test_stream_callback_journals_late_arrival_inversion(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.audio.voice_activity import SlotBinder
    from fakeyou.cli.join import build_transcript_callback
    from fakeyou.observability.journal import Journal
    from fakeyou.persistence.record import MeetingRecord, meeting_record_path

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, now=lambda: now, echo=False)
    path = meeting_record_path(tmp_path, "meeting-9")
    record = MeetingRecord(path, now=lambda: now)
    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None),
        binder=binder,
        voice_activity=None,
        per_stream_enabled=True,
    )
    later = TranscriptionFrame(
        "Second sentence spoken first.", "", "2026-10-06T01:00:02+00:00", finalized=True
    )
    later.transport_source = "slot-0"
    await callback(later)
    earlier = TranscriptionFrame(
        "First sentence arriving late.", "", "2026-10-06T01:00:01+00:00", finalized=True
    )
    earlier.transport_source = "slot-1"
    await callback(earlier)
    record.close()
    journal.close()
    rows = [json.loads(line) for line in lines]
    finals = [row for row in rows if row["event"] == "transcript_finalized"]
    assert [row["spoken_at"] for row in finals] == [
        "2026-10-06T01:00:02+00:00",
        "2026-10-06T01:00:01+00:00",
    ]
    inversions = [row for row in rows if row["event"] == "order_inversion_detected"]
    assert len(inversions) == 1
    assert inversions[0]["transcript_id"] == finals[1]["transcript_id"]
    assert inversions[0]["spoken_at"] == "2026-10-06T01:00:01+00:00"
    assert inversions[0]["emitted_max_spoken_at"] == "2026-10-06T01:00:02+00:00"


async def test_stream_callback_skips_inversion_for_ordered_arrivals(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.audio.voice_activity import SlotBinder
    from fakeyou.cli.join import build_transcript_callback
    from fakeyou.observability.journal import Journal
    from fakeyou.persistence.record import MeetingRecord, meeting_record_path

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, now=lambda: now, echo=False)
    path = meeting_record_path(tmp_path, "meeting-9")
    record = MeetingRecord(path, now=lambda: now)
    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    callback = build_transcript_callback(
        sinks=TranscriptSinks(journal, record, None),
        binder=binder,
        voice_activity=None,
        per_stream_enabled=True,
    )
    for stamp, slot, text in [
        ("2026-10-06T01:00:01+00:00", "slot-0", "First."),
        ("2026-10-06T01:00:01+00:00", "slot-1", "Same instant."),
        ("2026-10-06T01:00:02+00:00", "slot-0", "Second."),
    ]:
        frame = TranscriptionFrame(text, "", stamp, finalized=True)
        frame.transport_source = slot
        await callback(frame)
    record.close()
    journal.close()
    rows = [json.loads(line) for line in lines]
    assert len([row for row in rows if row["event"] == "transcript_finalized"]) == 3
    assert [row for row in rows if row["event"] == "order_inversion_detected"] == []


async def test_tagged_callback_stamps_capture_clock() -> None:
    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.audio.audio import _tagged

    seen: list[Any] = []

    async def record(frame: TranscriptionFrame) -> None:
        seen.append(frame)

    await _tagged("slot-0", record, clock=lambda: (7, 99.5))(
        TranscriptionFrame("hi", "", "2026-10-06T01:00:01+00:00")
    )
    assert (seen[0].capture_seq, seen[0].capture_ms) == (7, 99.5)
    await _tagged("slot-0", record)(TranscriptionFrame("hi", "", "2026-10-06T01:00:01+00:00"))
    assert getattr(seen[1], "capture_ms", 0.0) == 0.0


def test_attribute_stream_frame_returns_capture_ms() -> None:
    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.cli.join import attribute_stream_frame

    stamped = TranscriptionFrame("hi", "", "2026-10-06T01:00:01+00:00")
    stamped.transport_source = "slot-2"
    stamped_any: Any = stamped
    stamped_any.capture_ms = 55.5
    assert attribute_stream_frame(stamped, SlotBinder(capacity=4))[4] == 55.5
    plain = TranscriptionFrame("hi", "", "2026-10-06T01:00:01+00:00")
    plain.transport_source = "slot-2"
    assert attribute_stream_frame(plain, SlotBinder(capacity=4))[4] == 0.0


@pytest.mark.browser
async def test_stream_capture_supervisor_rescues_stalled_slot() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(1.5)
        await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await page.evaluate("() => window.__fakeyouStreamCapture.stallTestSlot(0)")
        await asyncio.sleep(3.5)
        rescued = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await asyncio.sleep(0.8)
        recovered = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    health = {entry["slot"]: entry for entry in rescued["health"]}
    assert health[0]["rescued"] == 1
    assert health[1]["rescued"] == 0
    assert health[0]["stalled"] is True
    assert health[1]["stalled"] is False
    assert {entry["slot"] for entry in recovered["streams"]} == {0, 1}
    assert all(entry["pcm_b64"] for entry in recovered["streams"])


@pytest.mark.browser
async def test_stream_capture_supervisor_releases_ended_track() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(1.5)
        await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await page.evaluate("() => window.__fakeyouStreamCapture.endTestTrack(0)")
        await asyncio.sleep(3.5)
        drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await asyncio.sleep(0.8)
        later = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    health = {entry["slot"]: entry for entry in drained["health"]}
    assert 0 not in health
    assert 1 in health
    assert 0 in drained["released"]
    assert health.get(0, {}).get("rescued", 0) == 0
    later_health = {entry["slot"]: entry for entry in later["health"]}
    assert 0 not in later_health
    assert 1 in later_health
    assert later["released"] == []


@pytest.mark.browser
async def test_stream_capture_supervisor_releases_stale_ended_track_without_rescue() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(1.5)
        await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await page.evaluate("() => window.__fakeyouStreamCapture.stallTestSlot(0)")
        await asyncio.sleep(3.5)
        drained = await page.evaluate(
            "() => { window.__fakeyouStreamCapture.endTestTrack(0);"
            " return window.__fakeyouStreamCapture.drain(); }"
        )
        await asyncio.sleep(0.8)
        later = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    health = {entry["slot"]: entry for entry in drained["health"]}
    assert 0 not in health
    assert 1 in health
    assert drained["released"] == [0]
    later_health = {entry["slot"]: entry for entry in later["health"]}
    assert 0 not in later_health
    assert later["released"] == []


@pytest.mark.browser
async def test_stream_capture_supervisor_releases_futile_slot() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(1.5)
        await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await page.evaluate("() => window.__fakeyouStreamCapture.poisonTestSlot(0)")
        await page.evaluate("() => window.__fakeyouStreamCapture.stallTestSlot(0)")
        await asyncio.sleep(3.5)
        first = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        first_health = {entry["slot"]: entry for entry in first["health"]}
        assert first_health[0]["stalled"] is True
        assert first_health[0]["rescued"] == 0
        assert first["released"] == []
        drained = first
        for _ in range(4):
            drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await asyncio.sleep(0.5)
        later = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    health = {entry["slot"]: entry for entry in drained["health"]}
    assert 0 not in health
    assert 1 in health
    assert drained["released"] == [0]
    later_health = {entry["slot"]: entry for entry in later["health"]}
    assert 0 not in later_health
    assert later["released"] == []


def test_parse_health_skips_malformed_entries() -> None:
    from fakeyou.audio.stream_capture import TapHealth, parse_health

    payload = {
        "health": [
            {"slot": 0, "stalled": False, "rescued": 2, "decode_fresh": True, "engine": "decode"},
            {"slot": "x", "stalled": False, "rescued": 0, "decode_fresh": False, "engine": "mstp"},
            {"slot": 1},
            "junk",
        ]
    }
    assert parse_health(payload) == {
        0: TapHealth(slot=0, stalled=False, rescued=2, decode_fresh=True, engine="decode")
    }
    assert parse_health({}) == {}


async def test_drain_loop_journals_slot_rescue_once() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    def payload(rescued: int) -> dict[str, Any]:
        return {
            "streams": [],
            "roster": [],
            "dropped": 0,
            "health": [
                {
                    "slot": 0,
                    "stalled": True,
                    "rescued": rescued,
                    "decode_fresh": False,
                    "engine": "mstp",
                }
            ],
        }

    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    registry, _ = _recording_registry(FakeClock().monotonic)
    page = meet_page([], evaluates=[payload(1), payload(1), payload(2)])
    loop = CaptureDrainLoop(
        page, registry, journal=journal, polling=DrainPolling(heartbeat_polls=1)
    )
    assert await loop.poll() is True
    assert await loop.poll() is True
    assert await loop.poll() is True
    rescues = [row for line in lines if (row := json.loads(line))["event"] == "tap_slot_rescued"]
    assert [(row["slot"], row["rescued"]) for row in rescues] == [(0, 1), (0, 2)]


async def test_drain_loop_journals_released_slots_as_dropped() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    payload = {
        "streams": [],
        "roster": [],
        "dropped": 0,
        "released": [0, 2],
    }
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    registry, _ = _recording_registry(FakeClock().monotonic)
    page = meet_page([], evaluates=[payload])
    loop = CaptureDrainLoop(page, registry, journal=journal)
    assert await loop.poll() is True
    drops = [row for line in lines if (row := json.loads(line))["event"] == "tap_slot_dropped"]
    assert [(row["source"], row["slot"]) for row in drops] == [("transport", 0), ("transport", 2)]


async def test_drain_loop_ignores_malformed_released() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    def payload(released: object) -> dict[str, Any]:
        return {"streams": [], "roster": [], "dropped": 0, "released": released}

    cases = [
        "nope",
        {"slot": 0},
        None,
        [0, "1", True, -1, 1.5, None, {"slot": 2}, 3],
    ]
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    registry, _ = _recording_registry(FakeClock().monotonic)
    page = meet_page([], evaluates=[payload(case) for case in cases])
    loop = CaptureDrainLoop(page, registry, journal=journal)
    for _ in cases:
        assert await loop.poll() is True
    drops = [row for line in lines if (row := json.loads(line))["event"] == "tap_slot_dropped"]
    assert [row["slot"] for row in drops] == [0, 3]


def test_parse_roster_names_keeps_id_name_and_drops_twin() -> None:
    payload = {
        "roster": [
            {"id": "dev-x", "name": "  Harun  "},
            {"id": "dev-t", "name": "Rani Twin"},
            {"id": "dev-blank", "name": "   "},
            {"name": "NoDevice"},
            {"id": "dev-noname"},
            "junk",
        ]
    }
    assert parse_roster_names(payload, twin_name="Rani Twin") == ({"id": "dev-x", "name": "Harun"},)
    assert parse_roster_names({"roster": []}) == ()
    assert parse_roster_names({}) == ()


async def test_capture_poll_binds_display_claim_from_roster() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    payload = {
        "streams": [
            {
                "slot": 0,
                "seq": 0,
                "csrcs": [111],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
        ],
        "roster": [
            {"id": "dev-x", "muted": False, "name": "Harun"},
            {"id": "dev-t", "muted": False, "name": "Rani Twin"},
        ],
        "dropped": 0,
    }
    page = meet_page([], evaluates=[payload, None])
    speakers = SpeakerRegistry()
    binder = SlotBinder(registry=speakers)
    registry, _ = _recording_registry(FakeClock().monotonic, binder=binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Rani Twin")
    assert await loop.poll() is True
    row = speakers.resolve(0)
    assert not isinstance(row, str)
    assert row.device == "dev-x"
    assert row.display_claim == "Harun"
    assert row.claim_confidence == 1.0


async def test_capture_drain_leaves_colliding_self_entry_unbound() -> None:
    from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.audio.voice_activity import SlotBinder

    payload = _collision_payload(["Twin"] * 4 + ["Host"])
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = _recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    assert binder.roster_size == 5
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        assert outcome.device != "devices/0"
