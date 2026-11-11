from pathlib import Path

from fakeyou.orchestration.perception import PerceptionArchive
from fakeyou.perception.percepts import ScreenReading
from fakeyou.persistence.record import MeetingRecord
from tests._perception_fakes import reading, shown_timeline

TIMELINE = ("Launch Timeline", ["Alpha: 12 January 2027"])
SECRET = "Receipt 1450-6289 Rp899,900"


def _dashboard() -> ScreenReading:
    return reading(
        "Cartesia Usage",
        text=SECRET,
        kind="dashboard",
        key_points=(SECRET,),
        figures=(("amount paid", "Rp899,900"),),
    )


async def test_the_record_keeps_the_text_of_a_dashboard_screen(tmp_path: Path) -> None:
    timeline = shown_timeline(TIMELINE)
    record = MeetingRecord(tmp_path / "record.jsonl")
    archive = PerceptionArchive(timeline, record=record, screens_dir=tmp_path / "screens")
    timeline.subscribe(archive)

    timeline.read_screen("screen#1", _dashboard())
    await archive.settle()

    raw = (tmp_path / "record.jsonl").read_text(encoding="utf-8")
    assert "Cartesia Usage" in raw
    assert SECRET in raw
    assert list((tmp_path / "screens").glob("*.jpg"))


async def test_a_noise_screen_is_left_out_of_the_record(tmp_path: Path) -> None:
    timeline = shown_timeline(TIMELINE)
    record = MeetingRecord(tmp_path / "record.jsonl")
    archive = PerceptionArchive(timeline, record=record, screens_dir=tmp_path / "screens")
    timeline.subscribe(archive)

    timeline.read_screen(
        "screen#1", reading("Meet Lobby", text="Allow microphone", kind="meeting_ui")
    )
    await archive.settle()

    raw = (tmp_path / "record.jsonl").read_text(encoding="utf-8")
    assert "Meet Lobby" not in raw
    assert list((tmp_path / "screens").glob("*.jpg")) == []


async def test_a_meeting_screen_still_saves_its_frame(tmp_path: Path) -> None:
    timeline = shown_timeline(TIMELINE)
    archive = PerceptionArchive(timeline, screens_dir=tmp_path / "screens")
    timeline.subscribe(archive)

    timeline.read_screen("screen#1", reading("Launch Timeline", text="Alpha: 12 January 2027"))
    await archive.settle()

    assert len(list((tmp_path / "screens").glob("*.jpg"))) == 1
