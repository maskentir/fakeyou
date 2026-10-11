import asyncio
import os
import re
import sys
import time
import unicodedata
import wave
from pathlib import Path
from typing import Any

import pytest
from pipecat.frames.frames import (
    EndFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.meta.stt import MetaSTTService
from pipecat.workers.runner import WorkerRunner
from pydantic import SecretStr

from fakeyou.audio.voice import SPEECH_LANGUAGES, MuseSTTOptions, create_muse_stt
from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import Settings, require_secret
from tests.fakes import FakeSTT


class _Collector(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.final: asyncio.Queue[TranscriptionFrame] = asyncio.Queue()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            await self.final.put(frame)
        await self.push_frame(frame, direction)


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return " ".join(text.split())


def _read_wav(path: Path) -> tuple[int, bytes]:
    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frames = audio.readframes(audio.getnframes())
    except (OSError, wave.Error) as error:
        raise ValueError(f"invalid WAV: {path}") from error
    if channels != 1 or sample_width != 2 or sample_rate != 24_000:
        raise ValueError("WAV must be mono 16-bit PCM at 24000 Hz")
    if not frames:
        raise ValueError("WAV has no audio frames")
    return sample_rate, frames


def test_wav_validation_rejects_wrong_sample_rate(tmp_path: Path) -> None:
    path = tmp_path / "wrong-rate.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\0\0" * 160)
    with pytest.raises(ValueError, match="24000"):
        _read_wav(path)


async def test_endpointing_harness_uses_configured_muse_model_url_and_language(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings.model_copy(
        update={
            "stt": settings.stt.model_copy(
                update={
                    "mode": "PUSH_TO_TALK",
                    "model": "custom-muse-model",
                    "url": "wss://muse.example.test/asr",
                    "api_key": SecretStr("test-key"),
                }
            ),
            "speech": settings.speech.model_copy(update={"language": "en"}),
        }
    )
    observed: dict[str, object] = {}

    monkeypatch.setattr(sys.modules[__name__], "Settings", lambda: configured)
    monkeypatch.setattr(sys.modules[__name__], "_read_wav", lambda _path: (24_000, b"\0\0"))
    monkeypatch.setenv("MUSE_TEST_AUDIO", "unused.wav")
    monkeypatch.setenv("MUSE_EXPECTED_TEXT", "Hello there")

    async def run_pipeline(
        stt: FrameProcessor, pcm: bytes, sample_rate: int, expected: str, secret: str | None
    ) -> None:
        observed.update(stt=stt, pcm=pcm, sample_rate=sample_rate, expected=expected, secret=secret)

    monkeypatch.setattr(sys.modules[__name__], "_run_pipeline", run_pipeline)

    await test_muse_endpointing_transcribes_realtime_wav()

    service = observed["stt"]
    assert isinstance(service, MetaSTTService)
    assert observed["pcm"] == b"\0\0"
    assert observed["sample_rate"] == 24_000
    assert observed["expected"] == "hello there"
    assert observed["secret"] == "test-key"

    stt_settings = service._settings
    assert stt_settings is not None
    assert stt_settings.model == "custom-muse-model"
    assert stt_settings.language == "English"
    assert stt_settings.mode == "ENDPOINTING"
    assert service._url == "wss://muse.example.test/asr"


async def _run_pipeline(
    stt: FrameProcessor,
    pcm: bytes,
    sample_rate: int,
    expected: str,
    secret: str | None = None,
) -> list[str]:
    collector = _Collector()
    task = PipelineWorker(
        Pipeline([stt, collector]),
        params=PipelineParams(audio_in_sample_rate=sample_rate, audio_out_sample_rate=sample_rate),
        idle_timeout_secs=60,
    )
    started = asyncio.Event()
    errors: list[str] = []

    async def on_pipeline_started(_: Any, __: Any) -> None:
        started.set()

    async def on_pipeline_error(_: Any, frame: ErrorFrame) -> None:
        errors.append(frame.error)

    worker: Any = task
    worker.event_handler("on_pipeline_started")(on_pipeline_started)
    worker.event_handler("on_pipeline_error")(on_pipeline_error)

    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(task)
    runner_task = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=20)
        chunk_size = sample_rate * 2 * 80 // 1000
        deadline = time.monotonic()
        for offset in range(0, len(pcm), chunk_size):
            chunk = pcm[offset : offset + chunk_size]
            await task.queue_frame(InputAudioRawFrame(chunk, sample_rate, 1))
            deadline = max(deadline, time.monotonic()) + len(chunk) / (sample_rate * 2)
            await asyncio.sleep(max(0, deadline - time.monotonic()))
        finals: list[str] = []
        try:
            async with asyncio.timeout(30):
                while _normalise(" ".join(finals)) != _normalise(expected):
                    frame = await collector.final.get()
                    assert frame.finalized
                    finals.append(frame.text)
        except TimeoutError as error:
            details = f"Final transcription timed out; errors={errors!r}; collected={finals!r}"
            if secret:
                details = details.replace(secret, "[redacted]")
            raise AssertionError(details) from error
        await task.queue_frame(EndFrame(reason="live smoke test complete"))
        await asyncio.wait_for(runner_task, timeout=15)
        if errors:
            details = "; ".join(errors)
            if secret:
                details = details.replace(secret, "[redacted]")
            raise AssertionError(details)
        assert _normalise(" ".join(finals)) == _normalise(expected)
        return finals
    finally:
        if not runner_task.done():
            await task.cancel(reason="live smoke test cleanup")
            await asyncio.wait_for(runner_task, timeout=15)


async def test_native_pipeline_harness_reaches_final_and_shutdown() -> None:
    finals = await _run_pipeline(
        FakeSTT(user_id="", finalized=(True,), text="hello world"),
        b"\0\0" * 240,
        24_000,
        "hello world",
    )
    assert finals == ["hello world"]


async def test_native_pipeline_harness_rejects_upstream_errors() -> None:
    with pytest.raises(AssertionError, match="provider unavailable"):
        await _run_pipeline(
            FakeSTT(
                user_id="", finalized=(True,), text="hello world", error="provider unavailable"
            ),
            b"\0\0" * 240,
            24_000,
            "hello world",
        )


@pytest.mark.live
async def test_muse_endpointing_transcribes_realtime_wav() -> None:
    settings = Settings()
    audio_name = os.environ.get("MUSE_TEST_AUDIO")
    expected = os.environ.get("MUSE_EXPECTED_TEXT")
    if not audio_name or not expected:
        pytest.skip("set MUSE_TEST_AUDIO and MUSE_EXPECTED_TEXT")
    try:
        api_key = require_secret(settings.stt.api_key, ConfigKey.STT_API_KEY)
    except FakeYouError as error:
        pytest.skip(str(error))
    sample_rate, pcm = _read_wav(Path(audio_name))
    expected_text = _normalise(expected)
    if not expected_text:
        pytest.fail("MUSE_EXPECTED_TEXT must contain text")

    stt = create_muse_stt(
        api_key=api_key,
        options=MuseSTTOptions(
            language=SPEECH_LANGUAGES[settings.speech.language][0],
            mode="ENDPOINTING",
            model=settings.stt.model,
        ),
        url=settings.stt.url,
        sample_rate=sample_rate,
    )
    await _run_pipeline(stt, pcm, sample_rate, expected_text, api_key)
