import asyncio
import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from fakeyou.foundation.polling import PollSchedule
from fakeyou.meet.participant_observer import (
    POLL_INTERVAL_SECONDS,
    ParticipantEvidenceWriter,
    ParticipantObserver,
    observer_epoch,
)
from fakeyou.observability.faults import ConsumerSink
from tests._meet_browser_fakes import HangingPage
from tests._persistence_fakes import RecordingJournal


class FakePage:
    def __init__(self, payloads: Iterable[dict[str, Any] | Exception]) -> None:
        self.payloads = list(payloads)
        self.calls = 0
        self.closed = False

    async def evaluate(self, _script: str) -> Any:
        self.calls += 1
        if not self.payloads:
            raise RuntimeError("page gone")
        payload = self.payloads[0]
        if len(self.payloads) > 1:
            self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload


def mining_result(*tiles: tuple[str, str, bool, bool], self_seen: bool = False) -> dict[str, Any]:
    return {
        "tiles": [
            {"key": key, "label": label, "self": self, "speaking": speaking}
            for key, label, self, speaking in tiles
        ],
        "people": [],
        "self_seen": self_seen,
    }


def test_poll_exception_does_not_stop_observer() -> None:
    async def run() -> None:
        page = FakePage(
            [
                mining_result(("a", "Budi", False, True)),
                RuntimeError("stale element"),
                mining_result(("a", "Budi", False, True)),
            ]
        )
        journal = RecordingJournal()
        persisted: list[Any] = []
        observer = ParticipantObserver(
            page,
            schedule=PollSchedule(monotonic=lambda: 100.0, interval_seconds=0.01),
            sinks=ConsumerSink(journal=journal),
            on_poll=lambda raw, _at: persisted.append(raw),
        )
        await observer.start()
        await asyncio.sleep(0.9)
        await observer.close()
        assert len(persisted) >= 2
        assert observer.polls >= 3
        assert observer.poll_failures == 1
        assert observer.last_poll_error == "RuntimeError"

    asyncio.run(run())


def test_close_is_idempotent_and_stops_polling() -> None:
    async def run() -> None:
        page = FakePage([mining_result(("a", "Budi", False, True))])
        observer = ParticipantObserver(
            page,
            schedule=PollSchedule(monotonic=lambda: 100.0, interval_seconds=POLL_INTERVAL_SECONDS),
            on_poll=lambda _raw, _at: None,
        )
        await observer.start()
        await asyncio.sleep(0.4)
        await observer.close()
        calls = page.calls
        await observer.close()
        await asyncio.sleep(0.4)
        assert page.calls == calls

    asyncio.run(run())


def test_hung_observe_evaluate_is_bounded_and_counted() -> None:
    async def run() -> None:
        page = HangingPage()
        journal = RecordingJournal()
        observer = ParticipantObserver(
            page,
            schedule=PollSchedule(monotonic=lambda: 100.0, interval_seconds=0.01),
            sinks=ConsumerSink(journal=journal),
            on_poll=lambda _raw, _at: None,
        )
        await observer.start()
        await asyncio.sleep(0.3)
        await observer.close()
        assert page.cancelled is True
        assert observer.poll_failures >= 1
        assert observer.last_poll_error == "TimeoutError"

    asyncio.run(run())


def test_evidence_strips_tile_markup_before_writing(tmp_path: Path) -> None:
    from fakeyou.meet.participant_observer import ParticipantEvidenceWriter

    path = tmp_path / "participants.jsonl"
    writer = ParticipantEvidenceWriter(path, epoch="e1", max_bytes=10**9)
    raw = {
        "tiles": [
            {
                "key": "a",
                "label": "Budi",
                "text": "hello",
                "html": "<div>heavy</div>",
                "cls": "x y z",
            }
        ],
        "people": ["Budi"],
        "selfSeen": True,
        "pageSpeaking": False,
    }

    assert writer.write(raw, 1000.0) is True

    [record] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert record["tiles"] == [{"key": "a", "label": "Budi", "text": "hello"}]
    assert record["people"] == ["Budi"]


def test_evidence_writer_skips_unchanged_tiles(tmp_path: Path) -> None:
    from fakeyou.meet.participant_observer import ParticipantEvidenceWriter

    path = tmp_path / "participants.jsonl"
    writer = ParticipantEvidenceWriter(path, epoch="e1", max_bytes=10**9)
    raw = {"tiles": [{"key": "a", "label": "Budi"}], "people": []}

    assert writer.write(raw, 1000.0) is True
    assert writer.write(raw, 1250.0) is False
    assert writer.write({"tiles": [{"key": "b", "label": "Sari"}], "people": []}, 1500.0) is True

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2


def test_evidence_writer_stops_at_the_size_cap_and_records_a_fault(tmp_path: Path) -> None:
    from fakeyou.meet.participant_observer import ParticipantEvidenceWriter
    from fakeyou.observability.faults import FaultReporter
    from tests._persistence_fakes import RecordingJournal

    path = tmp_path / "participants.jsonl"
    journal = RecordingJournal()
    writer = ParticipantEvidenceWriter(
        path, epoch="e1", max_bytes=10**9, faults=FaultReporter(journal=journal)
    )
    assert writer.write({"tiles": [{"key": "a", "label": "Budi"}], "people": []}, 1000.0)
    capped = ParticipantEvidenceWriter(
        path,
        epoch="e1",
        max_bytes=path.stat().st_size,
        faults=FaultReporter(journal=journal),
    )

    assert capped.write({"tiles": [{"key": "b", "label": "Sari"}], "people": []}, 1250.0) is False
    assert capped.write({"tiles": [{"key": "c", "label": "Ayu"}], "people": []}, 1500.0) is False

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 1
    assert len(journal.events("fault_recorded")) == 1


def test_poll_loop_runs_evidence_writes_off_the_event_loop() -> None:
    import threading

    async def run() -> None:
        main_thread = threading.get_ident()
        seen: list[int] = []
        payload = {"tiles": [{"key": "a", "label": "Budi"}], "people": []}
        page = FakePage([payload, payload, payload])
        observer = ParticipantObserver(
            page,
            schedule=PollSchedule(monotonic=time.monotonic, interval_seconds=0.01),
            on_poll=lambda _raw, _at: seen.append(threading.get_ident()),
        )
        await observer.start()
        await asyncio.sleep(0.15)
        await observer.close()
        assert seen != []
        assert all(thread != main_thread for thread in seen)

    asyncio.run(run())


def test_well_formed_observations_reach_the_evidence_sink() -> None:
    async def run() -> None:
        observation = mining_result(("a", "Budi", False, True), ("b", "Sam", False, False))
        page = FakePage([observation])
        persisted: list[tuple[Any, float]] = []
        observer = ParticipantObserver(
            page,
            schedule=PollSchedule(monotonic=lambda: 1.5, interval_seconds=POLL_INTERVAL_SECONDS),
            on_poll=lambda raw, at: persisted.append((raw, at)),
        )
        await observer.start()
        await asyncio.sleep(0.4)
        await observer.close()
        assert persisted
        assert persisted[0] == (observation, 1500.0)

    asyncio.run(run())


@pytest.mark.parametrize("payload", [None, [], {"people": []}, {"tiles": "a"}])
def test_malformed_observations_are_not_persisted(payload: Any) -> None:
    async def run() -> None:
        page = FakePage([payload])
        persisted: list[Any] = []
        observer = ParticipantObserver(
            page,
            schedule=PollSchedule(monotonic=lambda: 100.0, interval_seconds=0.01),
            on_poll=lambda raw, _at: persisted.append(raw),
        )
        await observer.start()
        await asyncio.sleep(0.1)
        await observer.close()
        assert observer.polls >= 1
        assert persisted == []

    asyncio.run(run())


def test_evidence_writer_appends_a_mining_record(tmp_path: Path) -> None:
    path = tmp_path / "m1" / "participants.jsonl"
    writer = ParticipantEvidenceWriter(path, epoch=observer_epoch("m1"), max_bytes=10**9)

    assert writer.write(
        {"tiles": [{"key": "a", "label": "Budi"}], "people": [], "selfSeen": True}, 1500.0
    )

    [record] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert record["epoch"] == "m1:e1"
    assert record["method"] == "dom_mining"
    assert record["mono_ms"] == 1500.0
    assert record["tiles"] == [{"key": "a", "label": "Budi"}]
    assert record["self_seen"] is True
    assert observer_epoch("m1", 2) == "m1:e2"
