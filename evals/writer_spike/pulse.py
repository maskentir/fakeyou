from __future__ import annotations

import ctypes
import time
from collections.abc import Callable
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    byref,
    c_char_p,
    c_int,
    c_int64,
    c_long,
    c_size_t,
    c_uint8,
    c_uint32,
    c_uint64,
    c_void_p,
)
from dataclasses import dataclass, field

RATE = 24000
CHANNELS = 1
SAMPLE_BYTES = 2
FRAME_SECONDS = 0.02
FRAME_BYTES = round(RATE * FRAME_SECONDS) * SAMPLE_BYTES * CHANNELS
PA_SAMPLE_S16LE = 3
CONTEXT_READY = 4
CONTEXT_FAILED = 5
CONTEXT_TERMINATED = 6
STREAM_READY = 2
STREAM_FAILED = 3
STREAM_TERMINATED = 4
STREAM_START_CORKED = 0x0001
STREAM_ADJUST_LATENCY = 0x2000
CONTEXT_NOAUTOSPAWN = 0x0001
UNSET = 0xFFFFFFFF
SEEK_RELATIVE = 0
SEEK_ABSOLUTE = 1
SEEK_RELATIVE_ON_READ = 2
PUMP_PAUSE_SECONDS = 0.0002


class SampleSpec(Structure):
    _fields_ = (("format", c_int), ("rate", c_uint32), ("channels", c_uint8))


class BufferAttr(Structure):
    _fields_ = (
        ("maxlength", c_uint32),
        ("tlength", c_uint32),
        ("prebuf", c_uint32),
        ("minreq", c_uint32),
        ("fragsize", c_uint32),
    )


class Timeval(Structure):
    _fields_ = (("seconds", c_long), ("microseconds", c_long))


class TimingInfo(Structure):
    _fields_ = (
        ("timestamp", Timeval),
        ("synchronized_clocks", c_int),
        ("sink_usec", c_uint64),
        ("source_usec", c_uint64),
        ("transport_usec", c_uint64),
        ("playing", c_int),
        ("write_index_corrupt", c_int),
        ("write_index", c_int64),
        ("read_index_corrupt", c_int),
        ("read_index", c_int64),
        ("configured_sink_usec", c_uint64),
        ("configured_source_usec", c_uint64),
        ("since_underrun", c_int64),
    )


NotifyCallback = CFUNCTYPE(None, c_void_p, c_void_p)
RequestCallback = CFUNCTYPE(None, c_void_p, c_size_t, c_void_p)
SuccessCallback = CFUNCTYPE(None, c_void_p, c_int, c_void_p)
EventCallback = CFUNCTYPE(None, c_void_p, c_char_p, c_void_p, c_void_p)

lib = ctypes.CDLL("libpulse.so.0")


def _declare(name: str, restype: object, *argtypes: object) -> None:
    function = getattr(lib, name)
    function.restype = restype
    function.argtypes = list(argtypes)


_declare("pa_mainloop_new", c_void_p)
_declare("pa_mainloop_get_api", c_void_p, c_void_p)
_declare("pa_mainloop_iterate", c_int, c_void_p, c_int, POINTER(c_int))
_declare("pa_mainloop_free", None, c_void_p)
_declare("pa_context_new", c_void_p, c_void_p, c_char_p)
_declare("pa_context_connect", c_int, c_void_p, c_char_p, c_int, c_void_p)
_declare("pa_context_get_state", c_int, c_void_p)
_declare("pa_context_is_pending", c_int, c_void_p)
_declare("pa_context_errno", c_int, c_void_p)
_declare("pa_context_disconnect", None, c_void_p)
_declare("pa_context_unref", None, c_void_p)
_declare("pa_context_set_state_callback", None, c_void_p, NotifyCallback, c_void_p)
_declare("pa_stream_new", c_void_p, c_void_p, c_char_p, POINTER(SampleSpec), c_void_p)
_declare(
    "pa_stream_connect_playback",
    c_int,
    c_void_p,
    c_char_p,
    POINTER(BufferAttr),
    c_int,
    c_void_p,
    c_void_p,
)
_declare("pa_stream_connect_record", c_int, c_void_p, c_char_p, POINTER(BufferAttr), c_int)
_declare("pa_stream_get_state", c_int, c_void_p)
_declare("pa_stream_get_index", c_uint32, c_void_p)
_declare("pa_stream_writable_size", c_size_t, c_void_p)
_declare("pa_stream_write", c_int, c_void_p, c_char_p, c_size_t, c_void_p, c_int64, c_int)
_declare("pa_stream_peek", c_int, c_void_p, POINTER(c_void_p), POINTER(c_size_t))
_declare("pa_stream_drop", c_int, c_void_p)
_declare("pa_stream_cork", c_void_p, c_void_p, c_int, SuccessCallback, c_void_p)
_declare("pa_stream_flush", c_void_p, c_void_p, SuccessCallback, c_void_p)
_declare("pa_stream_update_timing_info", c_void_p, c_void_p, SuccessCallback, c_void_p)
_declare("pa_stream_get_timing_info", POINTER(TimingInfo), c_void_p)
_declare("pa_stream_disconnect", c_int, c_void_p)
_declare("pa_stream_unref", None, c_void_p)
_declare("pa_operation_unref", None, c_void_p)
for _hook in (
    "state",
    "started",
    "underflow",
    "overflow",
    "suspended",
    "moved",
    "latency_update",
    "buffer_attr",
):
    _declare(f"pa_stream_set_{_hook}_callback", None, c_void_p, NotifyCallback, c_void_p)
_declare("pa_stream_set_write_callback", None, c_void_p, RequestCallback, c_void_p)
_declare("pa_stream_set_read_callback", None, c_void_p, RequestCallback, c_void_p)
_declare("pa_stream_set_event_callback", None, c_void_p, EventCallback, c_void_p)

NOTIFY_HOOKS = (
    "state",
    "started",
    "underflow",
    "overflow",
    "suspended",
    "moved",
    "latency_update",
    "buffer_attr",
)


def now_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


class PulseError(RuntimeError):
    pass


@dataclass(slots=True)
class Timing:
    received_at: int
    read_index: int
    read_index_corrupt: bool
    write_index: int
    write_index_corrupt: bool
    playing: bool
    sink_usec: int
    configured_sink_usec: int


class Client:
    def __init__(self, name: str, server: str | None = None) -> None:
        self.iterations = 0
        self.callbacks = 0
        self._keep: list[object] = []
        self.loop = lib.pa_mainloop_new()
        self.context = lib.pa_context_new(lib.pa_mainloop_get_api(self.loop), name.encode())
        self._hook(lib.pa_context_set_state_callback, NotifyCallback, self.context)
        target = None if server is None else server.encode()
        if lib.pa_context_connect(self.context, target, CONTEXT_NOAUTOSPAWN, None) < 0:
            raise PulseError(f"context connect failed: {lib.pa_context_errno(self.context)}")
        self.pump_until(lambda: self.state() == CONTEXT_READY, 5.0)

    def _hook(self, setter: Callable[..., None], kind: type, target: int) -> None:
        def counted(*_arguments: object) -> None:
            self.callbacks += 1

        callback = kind(counted)
        self._keep.append(callback)
        setter(target, callback, None)

    def keep(self, callback: object) -> None:
        self._keep.append(callback)

    def state(self) -> int:
        return int(lib.pa_context_get_state(self.context))

    def pending(self) -> bool:
        return bool(lib.pa_context_is_pending(self.context))

    def iterate(self) -> int:
        self.iterations += 1
        return int(lib.pa_mainloop_iterate(self.loop, 0, None))

    def pump(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.iterate() <= 0:
                time.sleep(PUMP_PAUSE_SECONDS)

    def pump_until(self, done: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not done():
            if time.monotonic() >= deadline:
                return False
            if self.state() in (CONTEXT_FAILED, CONTEXT_TERMINATED):
                raise PulseError("context lost")
            if self.iterate() <= 0:
                time.sleep(PUMP_PAUSE_SECONDS)
        return True

    def close(self) -> None:
        lib.pa_context_disconnect(self.context)
        lib.pa_context_unref(self.context)
        lib.pa_mainloop_free(self.loop)


@dataclass(slots=True)
class Playback:
    client: Client
    name: str
    sink: str = "agent_output"
    corked: bool = False
    target_seconds: float = 0.04
    prebuffer_seconds: float = 0.0
    stream: int = field(init=False)
    timings: list[Timing] = field(init=False, default_factory=list)
    acknowledged: list[int] = field(init=False, default_factory=list)
    hooks: dict[str, list[int]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        spec = SampleSpec(PA_SAMPLE_S16LE, RATE, CHANNELS)
        self.stream = lib.pa_stream_new(self.client.context, self.name.encode(), byref(spec), None)
        for hook in NOTIFY_HOOKS:
            self._notify(hook)
        self._sized("write")
        attr = BufferAttr(
            UNSET,
            round(RATE * self.target_seconds) * SAMPLE_BYTES * CHANNELS,
            round(RATE * self.prebuffer_seconds) * SAMPLE_BYTES * CHANNELS,
            UNSET,
            UNSET,
        )
        flags = STREAM_START_CORKED if self.corked else 0
        if (
            lib.pa_stream_connect_playback(
                self.stream, self.sink.encode(), byref(attr), flags, None, None
            )
            < 0
        ):
            raise PulseError("playback connect failed")
        if not self.client.pump_until(lambda: self.state() == STREAM_READY, 5.0):
            raise PulseError("playback stream did not become ready")

    def _notify(self, hook: str) -> None:
        moments = self.hooks.setdefault(hook, [])

        def called(_stream: int, _userdata: int) -> None:
            self.client.callbacks += 1
            moments.append(now_ns())

        callback = NotifyCallback(called)
        self.client.keep(callback)
        getattr(lib, f"pa_stream_set_{hook}_callback")(self.stream, callback, None)

    def _sized(self, hook: str) -> None:
        moments = self.hooks.setdefault(hook, [])

        def called(_stream: int, _size: int, _userdata: int) -> None:
            self.client.callbacks += 1
            moments.append(now_ns())

        callback = RequestCallback(called)
        self.client.keep(callback)
        getattr(lib, f"pa_stream_set_{hook}_callback")(self.stream, callback, None)

    def state(self) -> int:
        return int(lib.pa_stream_get_state(self.stream))

    def index(self) -> int:
        return int(lib.pa_stream_get_index(self.stream))

    def writable(self) -> int:
        return int(lib.pa_stream_writable_size(self.stream))

    def write(self, pcm: bytes, seek: int = SEEK_RELATIVE_ON_READ, offset: int = 0) -> int:
        return int(lib.pa_stream_write(self.stream, pcm, len(pcm), None, offset, seek))

    def _acknowledged(self, moments: list[int]) -> object:
        def called(_stream: int, _success: int, _userdata: int) -> None:
            self.client.callbacks += 1
            moments.append(now_ns())

        callback = SuccessCallback(called)
        self.client.keep(callback)
        return callback

    def cork(self, *, corked: bool) -> list[int]:
        moments: list[int] = []
        operation = lib.pa_stream_cork(self.stream, int(corked), self._acknowledged(moments), None)
        lib.pa_operation_unref(operation)
        return moments

    def flush(self) -> list[int]:
        moments: list[int] = []
        operation = lib.pa_stream_flush(self.stream, self._acknowledged(moments), None)
        lib.pa_operation_unref(operation)
        return moments

    def request_timing(self) -> list[int]:
        moments: list[int] = []

        def called(_stream: int, _success: int, _userdata: int) -> None:
            self.client.callbacks += 1
            received = now_ns()
            moments.append(received)
            info = lib.pa_stream_get_timing_info(self.stream)
            if info:
                value = info.contents
                self.timings.append(
                    Timing(
                        received,
                        int(value.read_index),
                        bool(value.read_index_corrupt),
                        int(value.write_index),
                        bool(value.write_index_corrupt),
                        bool(value.playing),
                        int(value.sink_usec),
                        int(value.configured_sink_usec),
                    )
                )

        callback = SuccessCallback(called)
        self.client.keep(callback)
        operation = lib.pa_stream_update_timing_info(self.stream, callback, None)
        if operation:
            lib.pa_operation_unref(operation)
        return moments

    def close(self) -> None:
        lib.pa_stream_disconnect(self.stream)
        self.client.pump_until(lambda: self.state() in (STREAM_TERMINATED, STREAM_FAILED), 2.0)
        lib.pa_stream_unref(self.stream)


@dataclass(slots=True)
class Recorder:
    client: Client
    source: str
    fragment_bytes: int
    sink: Callable[[int, bytes], None]
    stream: int = field(init=False)

    def __post_init__(self) -> None:
        spec = SampleSpec(PA_SAMPLE_S16LE, RATE, CHANNELS)
        self.stream = lib.pa_stream_new(self.client.context, b"observer", byref(spec), None)
        callback = RequestCallback(self._readable)
        self.client.keep(callback)
        lib.pa_stream_set_read_callback(self.stream, callback, None)
        attr = BufferAttr(UNSET, UNSET, UNSET, UNSET, self.fragment_bytes)
        if (
            lib.pa_stream_connect_record(
                self.stream, self.source.encode(), byref(attr), STREAM_ADJUST_LATENCY
            )
            < 0
        ):
            raise PulseError("record connect failed")
        ready = self.client.pump_until(
            lambda: int(lib.pa_stream_get_state(self.stream)) == STREAM_READY, 5.0
        )
        if not ready:
            raise PulseError("record stream did not become ready")

    def _readable(self, _stream: int, _size: int, _userdata: int) -> None:
        received = now_ns()
        data = c_void_p()
        size = c_size_t()
        while True:
            if lib.pa_stream_peek(self.stream, byref(data), byref(size)) < 0 or size.value == 0:
                return
            chunk = ctypes.string_at(data.value, size.value) if data.value else b"\x00" * size.value
            self.sink(received, chunk)
            lib.pa_stream_drop(self.stream)
