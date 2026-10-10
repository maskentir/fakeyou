import asyncio
import json
from pathlib import Path

import pytest

from fakeyou.meet.participant_observer import (
    ParticipantObserver,
    append_participant_evidence,
    observer_epoch,
)


class FakePage:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = 0
        self.closed = False

    async def evaluate(self, _script):
        self.calls += 1
        if not self.payloads:
            raise RuntimeError("page gone")
        payload = self.payloads[0]
        if len(self.payloads) > 1:
            self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return payload


def mining_result(*tiles, self_seen=False):
    return {
        "tiles": [
            {"key": key, "label": label, "self": self, "speaking": speaking}
            for key, label, self, speaking in tiles
        ],
        "people": [],
        "self_seen": self_seen,
    }


def test_empty_roster_yields_no_samples():
    async def run():
        page = FakePage([mining_result()])
        observer = ParticipantObserver(page, epoch="e1", monotonic=lambda: 100.0)
        await observer.start()
        await asyncio.sleep(0.6)
        samples, roster = observer.snapshot()
        await observer.close()
        assert dict(roster) == {}
        assert all(sample.active_ids == () for sample in samples)

    asyncio.run(run())


def test_roster_and_active_speaker_snapshot():
    async def run():
        page = FakePage(
            [
                mining_result(
                    ("a", "Budi", False, True),
                    ("b", "Rani", False, False),
                    self_seen=True,
                )
            ]
        )
        observer = ParticipantObserver(page, epoch="e1", monotonic=lambda: 100.0)
        await observer.start()
        await asyncio.sleep(0.6)
        samples, roster = observer.snapshot()
        await observer.close()
        assert set(roster) == {"a", "b"}
        assert roster["a"].display_name == "Budi"
        assert samples
        assert samples[-1].active_ids == ("a",)

    asyncio.run(run())


def test_duplicate_names_keep_distinct_ids():
    async def run():
        page = FakePage([mining_result(("a", "Sam", False, False), ("b", "Sam", False, True))])
        observer = ParticipantObserver(page, epoch="e1", monotonic=lambda: 100.0)
        await observer.start()
        await asyncio.sleep(0.6)
        samples, roster = observer.snapshot()
        await observer.close()
        assert set(roster) == {"a", "b"}
        assert samples[-1].active_ids == ("b",)

    asyncio.run(run())


def test_participant_leave_keeps_history_but_drops_roster():
    async def run():
        page = FakePage(
            [
                mining_result(("a", "Budi", False, True)),
                mining_result(("a", "Budi", False, True)),
                mining_result(),
            ]
        )
        observer = ParticipantObserver(page, epoch="e1", monotonic=lambda: 100.0)
        await observer.start()
        await asyncio.sleep(0.9)
        samples, roster = observer.snapshot()
        await observer.close()
        assert dict(roster) == {}
        assert any(sample.active_ids == ("a",) for sample in samples)

    asyncio.run(run())


def test_poll_exception_does_not_stop_observer():
    async def run():
        page = FakePage(
            [
                mining_result(("a", "Budi", False, True)),
                RuntimeError("stale element"),
                mining_result(("a", "Budi", False, True)),
            ]
        )
        observer = ParticipantObserver(
            page, epoch="e1", monotonic=lambda: 100.0, poll_interval_seconds=0.01
        )
        await observer.start()
        await asyncio.sleep(0.9)
        samples, roster = observer.snapshot()
        await observer.close()
        assert set(roster) == {
            "a",
        }
        assert samples[-1].active_ids == ("a",)
        assert observer.polls >= 3
        assert observer.poll_failures == 1
        assert observer.last_poll_error == "RuntimeError"

    asyncio.run(run())


def test_close_is_idempotent_and_stops_polling():
    async def run():
        page = FakePage([mining_result(("a", "Budi", False, True))])
        observer = ParticipantObserver(page, epoch="e1", monotonic=lambda: 100.0)
        await observer.start()
        await asyncio.sleep(0.4)
        await observer.close()
        calls = page.calls
        await observer.close()
        await asyncio.sleep(0.4)
        assert page.calls == calls

    asyncio.run(run())


def test_epoch_reset_clears_timeline_and_roster():
    async def run():
        page = FakePage([mining_result(("a", "Budi", False, True))])
        observer = ParticipantObserver(page, epoch="e1", monotonic=lambda: 100.0)
        await observer.start()
        await asyncio.sleep(0.4)
        observer.reset_epoch("e2")
        samples, roster = observer.snapshot()
        assert samples == ()
        assert dict(roster) == {}
        await asyncio.sleep(0.4)
        samples, _ = observer.snapshot()
        assert samples and all(sample.epoch == "e2" for sample in samples)
        await observer.close()

    asyncio.run(run())


def test_evidence_appends_mining_record(tmp_path):
    path = Path(tmp_path) / "m1" / "participants.jsonl"
    append_participant_evidence(
        path,
        {"tiles": [{"key": "a", "label": "Budi"}], "people": [], "selfSeen": True},
        1500.0,
        epoch=observer_epoch("m1"),
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["epoch"] == "m1:e1"
    assert record["method"] == "dom_mining"
    assert record["mono_ms"] == 1500.0
    assert record["tiles"] == [{"key": "a", "label": "Budi"}]
    assert record["self_seen"] is True
    assert observer_epoch("m1", 2) == "m1:e2"


def test_snapshot_is_immutable():
    async def run():
        page = FakePage([mining_result(("a", "Budi", False, True))])
        observer = ParticipantObserver(page, epoch="e1", monotonic=lambda: 100.0)
        await observer.start()
        await asyncio.sleep(0.4)
        samples, roster = observer.snapshot()
        with pytest.raises(TypeError):
            roster["x"] = roster["a"]
        assert isinstance(samples, tuple)
        await observer.close()

    asyncio.run(run())
