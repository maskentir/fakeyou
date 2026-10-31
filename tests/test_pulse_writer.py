import threading

import pytest
from pasimple import PaSimpleError

import fakeyou.audio.pulse_writer as pulse_writer
from fakeyou.audio.playback import PlaybackError
from fakeyou.audio.pulse_writer import PulseBuffer, PulseWriter


class FakeStream:
    opened: list["FakeStream"]
    fail_write = False

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs
        self.calls: list[tuple[str, str]] = []
        self.written: list[bytes] = []
        FakeStream.opened.append(self)

    def _note(self, name: str) -> None:
        self.calls.append((name, threading.current_thread().name))

    def write(self, data: bytes) -> None:
        self._note("write")
        if FakeStream.fail_write:
            raise PaSimpleError("server gone")
        self.written.append(data)

    def drain(self) -> None:
        self._note("drain")

    def flush(self) -> None:
        self._note("flush")

    def close(self) -> None:
        self._note("close")

    def get_latency(self) -> int:
        return 0


@pytest.fixture
def streams(monkeypatch: pytest.MonkeyPatch) -> list[FakeStream]:
    FakeStream.opened = []
    FakeStream.fail_write = False
    monkeypatch.setattr(pulse_writer, "PaSimple", FakeStream)
    return FakeStream.opened


async def test_the_stream_is_primed_at_construction_with_a_short_buffer(
    streams: list[FakeStream],
) -> None:
    writer = PulseWriter(sink="agent_output", rate=24000)

    await writer.drain()
    [stream] = streams
    assert stream.kwargs["device_name"] == "agent_output"
    assert stream.kwargs["tlength"] == 1920
    assert stream.kwargs["prebuf"] == 960
    assert stream.written == [b"\x00" * 960]
    await writer.write(b"\x01\x00" * 480)
    await writer.write(b"\x02\x00" * 480)

    assert stream.written == [b"\x00" * 960, b"\x01\x00" * 480, b"\x02\x00" * 480]
    assert writer.buffer_seconds == pytest.approx(0.04)


async def test_a_failed_prime_leaves_the_writer_usable(streams: list[FakeStream]) -> None:
    FakeStream.fail_write = True
    writer = PulseWriter(sink="agent_output", rate=24000)

    await writer.drain()
    FakeStream.fail_write = False
    await writer.write(b"\x00\x00")

    first, second = streams
    assert first.calls[-1][0] == "close"
    assert second.written == [b"\x00\x00"]


async def test_every_stream_call_runs_on_one_writer_thread(streams: list[FakeStream]) -> None:
    writer = PulseWriter(sink="agent_output", rate=24000, buffer=PulseBuffer(0.05, 0.01))

    await writer.write(b"\x00\x00" * 10)
    await writer.drain()
    await writer.close()

    [stream] = streams
    assert [name for name, _thread in stream.calls] == [
        "write",
        "write",
        "drain",
        "flush",
        "close",
    ]
    assert len({thread for _name, thread in stream.calls}) == 1
    assert stream.calls[0][1] != threading.current_thread().name
    assert stream.kwargs["tlength"] == 2400


async def test_a_failed_write_raises_and_the_next_write_opens_a_new_stream(
    streams: list[FakeStream],
) -> None:
    writer = PulseWriter(sink="agent_output", rate=24000)
    FakeStream.fail_write = True

    with pytest.raises(PlaybackError, match="server gone"):
        await writer.write(b"\x00\x00")

    FakeStream.fail_write = False
    await writer.write(b"\x00\x00")
    first, second = streams
    assert first.calls[-1][0] == "close"
    assert second.written == [b"\x00\x00"]


async def test_a_closed_writer_refuses_writes(streams: list[FakeStream]) -> None:
    writer = PulseWriter(sink="agent_output", rate=24000)
    await writer.write(b"\x00\x00")
    await writer.close()
    await writer.close()

    with pytest.raises(PlaybackError, match="closed"):
        await writer.write(b"\x00\x00")
    assert [name for name, _thread in streams[0].calls].count("close") == 1
