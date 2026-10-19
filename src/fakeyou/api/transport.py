from typing import cast

from fastapi import Request

from fakeyou.transport.streams import StreamTransport


def shared_stream_transport(request: Request) -> StreamTransport:
    state = request.app.state
    if state.stream_transport is None:
        state.stream_transport = StreamTransport.from_settings(state.settings)
    return cast(StreamTransport, state.stream_transport)
