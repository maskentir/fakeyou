import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.audio import PurgeReport, visual_purge_expired
from fakeyou.config.settings import Settings, VisualSettings
from fakeyou.conversation.conversation import _request_messages
from fakeyou.conversation.session import SessionConfig
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.meet.meet import DEFAULT_MEET_SIGNALS
from fakeyou.memory.visual_hydrate import VisualHydrateMiddleware
from fakeyou.orchestration.lifecycle import MeetingLifecycle
from fakeyou.orchestration.runtime import _MeetingWorker
from fakeyou.visual.visual_caption import VisualCaptioner
from fakeyou.visual.visual_observer import VisualObserver
from fakeyou.visual.visual_store import VisualFrame, VisualStore
from tests.fakes import FakeClock, RecordingJournal

DAY = 86400.0
AT = "2026-10-09T03:10:00+00:00"
SPOKEN_AT = "2026-10-09T03:12:00+00:00"
PRESENT_LABELS = [
    "Alex is presenting",
    "Presentation",
    "Unpin Alex presentation view",
]


def aged_file(path: Path, age_days: float, payload: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))
    return path.stat().st_size


def visual_dir(root: Path, meeting: str) -> Path:
    directory = root / meeting / "visual"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class SilentPage:
    def __init__(self) -> None:
        self.evaluates = 0

    async def evaluate(self, _script: str) -> dict[str, Any]:
        self.evaluates += 1
        return {"labels": list(PRESENT_LABELS), "page_ms": 1700000000000.0}

    def locator(self, _selector: str) -> Any:
        raise AssertionError("capture attempted while visual is disabled")


class DeadModel:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, _messages: Any) -> Any:
        self.calls += 1
        raise AssertionError("caption attempted while visual is disabled")


def tagged_message(refs: tuple[str, ...]) -> Any:
    event = TranscriptEvent(
        id="v1",
        timestamp=SPOKEN_AT,
        source="meeting_audio",
        text="What shows revenue?",
        finalized=True,
        participant_id="unknown",
        speaker_evidence_id=None,
        visual_refs=refs,
    )
    (message,) = _request_messages([event])
    return message


def test_purge_removes_expired_png(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "meetings"
    directory = visual_dir(root, "meeting-old")
    freed = aged_file(directory / "shot-old.jpg", 9.0, b"\xff\xd8old-frame" * 64)
    aged_file(directory / "shot-new.jpg", 1.0, b"\xff\xd8new-frame" * 64)
    aged_file(directory / "visuals.jsonl", 1.0, b'{"visual_id":"fresh"}\n')

    report = visual_purge_expired(root=root, days=None, settings=settings)

    assert isinstance(report, PurgeReport)
    assert report.freed_bytes == freed
    assert report.removed == ["meeting-old/shot-old.jpg"]
    assert not (directory / "shot-old.jpg").exists()
    assert (directory / "shot-new.jpg").exists()
    assert (directory / "visuals.jsonl").exists()
    assert "freed_bytes" in capsys.readouterr().out


def test_text_kept_30d(settings: Settings, tmp_path: Path) -> None:
    root = tmp_path / "meetings"
    directory = visual_dir(root, "meeting-text")
    aged_file(directory / "shot-old.jpg", 9.0, b"\xff\xd8old-frame" * 64)
    aged_file(directory / "frame-aaaa.caption.json", 12.0, b'{"text":"stale caption"}')
    aged_file(directory / "frame-bbbb.caption.json", 40.0, b'{"text":"expired caption"}')

    report = visual_purge_expired(root=root, days=None, settings=settings)

    assert (directory / "frame-aaaa.caption.json").exists()
    assert not (directory / "frame-bbbb.caption.json").exists()
    assert not (directory / "shot-old.jpg").exists()
    assert report.kept == 1


def test_purge_rejects_bad_input(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(FakeYouError, match="positive") as rejected:
        visual_purge_expired(root=tmp_path, days=0, settings=settings)
    assert rejected.value.code is ErrorCode.AUDIO_RETENTION_DAYS_NOT_POSITIVE
    with pytest.raises(FakeYouError) as missing:
        visual_purge_expired(root=tmp_path / "no-such-root", days=None, settings=settings)
    assert missing.value.code is ErrorCode.VISUAL_ROOT_MISSING
    assert missing.value.exit_code is ExitCode.INPUT_REJECTED


async def test_flag_off_disables_everything(settings: Settings, tmp_path: Path) -> None:
    assert settings.visual.enabled is False
    assert VisualSettings().enabled is False

    page = SilentPage()
    journal = RecordingJournal()
    store = VisualStore(tmp_path / "visual", journal=journal)
    observer = VisualObserver(page, store, journal=journal, enabled=False)
    assert await observer.poll() is None
    assert await observer.poll() is None
    assert page.evaluates == 0
    assert store.frames == ()
    assert journal.events("visual_captured") == []

    frame = VisualFrame(
        frame_id="1700000000000-deadbeef",
        captured_page_ms=1700000000000.0,
        captured_at=AT,
        sha256="ab" * 32,
        path=str(tmp_path / "ghost.jpg"),
        thumb_path=str(tmp_path / "ghost.thumb.jpg"),
        dhash=0,
    )
    model = DeadModel()
    captioner = VisualCaptioner(model, settings=VisualSettings())
    assert await captioner.caption_frame(frame) is None
    assert model.calls == 0

    middleware = VisualHydrateMiddleware(store, captioner, enabled=False)
    message = tagged_message(("ghost-frame",))
    request = SimpleNamespace(messages=[message])
    seen: dict[str, Any] = {}

    async def handler(current: Any) -> Any:
        seen["request"] = current
        return SimpleNamespace(result=[AIMessage(content="ok")])

    response = await middleware.awrap_model_call(request, handler)
    assert seen["request"] is request
    assert response.result[0].content == "ok"
    assert message.additional_kwargs.get("visual_evidence") is None
    assert await middleware.abefore_model({"messages": [message]}, None) is None
    echo = AIMessage(
        content=[
            {"type": "text", "text": "Revenue grew."},
            {"type": "image", "base64": "aGVsbG8=", "mime_type": "image/jpeg"},
        ]
    )
    assert middleware.after_model({"messages": [echo]}, None) is None

    lifecycle = MeetingLifecycle("owner-1", "twin-1", "meeting-1", InMemorySaver())
    await lifecycle.initialize()
    clock = FakeClock()
    worker = _MeetingWorker(
        SessionConfig(
            owner_id="owner-1",
            twin_id="twin-1",
            meeting_id="meeting-1",
            link="https://meet.example.com/abc-defg-hij",
            twin_name="Twin",
        ),
        lifecycle,
        SimpleNamespace(),
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
        visual_store=store,
    )
    await worker.start_visual_observer()
    assert worker.visual_observer is None


async def test_flag_on_starts_observer(tmp_path: Path) -> None:
    lifecycle = MeetingLifecycle("owner-1", "twin-1", "meeting-1", InMemorySaver())
    await lifecycle.initialize()
    clock = FakeClock()
    worker = _MeetingWorker(
        SessionConfig(
            owner_id="owner-1",
            twin_id="twin-1",
            meeting_id="meeting-1",
            link="https://meet.example.com/abc-defg-hij",
            twin_name="Twin",
            visual_enabled=True,
        ),
        lifecycle,
        SimpleNamespace(),
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
        visual_store=VisualStore(tmp_path / "visual"),
    )
    await worker.start_visual_observer()
    assert worker.visual_observer is not None
    await worker.close_visual_observer()
    assert worker.visual_observer is None
