PA_STREAM_PLAYBACK: int
PA_STREAM_RECORD: int
PA_SAMPLE_S16LE: int

class PaSimpleError(Exception): ...

class PaSimple:
    def __init__(
        self,
        direction: int,
        format: int,
        channels: int,
        rate: int,
        app_name: str = ...,
        stream_name: str | None = ...,
        server_name: str | None = ...,
        device_name: str | None = ...,
        maxlength: int = ...,
        tlength: int = ...,
        prebuf: int = ...,
        minreq: int = ...,
        fragsize: int = ...,
    ) -> None: ...
    def write(self, data: bytes) -> None: ...
    def drain(self) -> None: ...
    def flush(self) -> None: ...
    def get_latency(self) -> int: ...
    def close(self) -> None: ...
