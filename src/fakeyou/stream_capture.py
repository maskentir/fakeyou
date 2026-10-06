from importlib.resources import files
from typing import Any

from pipecat.frames.frames import InputAudioRawFrame, StartFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_transport import TransportParams


def load_capture_script() -> str:
    return (files("fakeyou") / "assets" / "stream_capture.js").read_text(encoding="utf-8")


class StreamAudioInput(BaseInputTransport):
    def __init__(self, params: TransportParams, **kwargs: Any) -> None:
        super().__init__(params, **kwargs)

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def feed(self, audio: bytes, sample_rate: int = 16000) -> None:
        await self.push_audio_frame(
            InputAudioRawFrame(audio=audio, sample_rate=sample_rate, num_channels=1)
        )
