import asyncio
import os
import time
import wave

import pytest

from fakeyou.audio.audio import vad_params
from fakeyou.audio.deaf import CaptureActivity
from fakeyou.audio.graph import AudioGraph, GraphParts, PcmQueue
from fakeyou.audio.vad import VadAnalyzer, VadController
from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import Settings, require_secret
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.transcription.contract import SpeechSinks, TranscriptionError
from fakeyou.transcription.muse import MuseClient, MuseOptions

CHUNK_BYTES = 1280 * 2


@pytest.mark.live
async def test_two_voice_clip_through_the_audio_graph_yields_attributed_turns() -> None:
    audio_name = os.environ.get("MUSE_DIARIZATION_AUDIO")
    if not audio_name:
        pytest.skip("set MUSE_DIARIZATION_AUDIO to a 16 kHz mono WAV with two voices")
    settings = Settings()
    try:
        api_key = require_secret(settings.stt.api_key, ConfigKey.STT_API_KEY)
    except FakeYouError as error:
        pytest.skip(str(error))
    with wave.open(audio_name, "rb") as source_file:
        assert source_file.getframerate() == 16000
        assert source_file.getnchannels() == 1
        pcm = source_file.readframes(source_file.getnframes())

    finals: list[TranscriptUpdate] = []
    lost: list[TranscriptionError] = []
    speech_starts: list[bool] = []

    async def collect(update: TranscriptUpdate) -> None:
        finals.append(update)

    async def ignore(update: TranscriptUpdate) -> None:
        del update

    async def on_vad(*, active: bool) -> None:
        speech_starts.append(active)

    client = MuseClient(
        api_key=api_key,
        options=MuseOptions(
            language="en", mode="DIARIZATION", model=settings.stt.model, url=settings.stt.url
        ),
        sinks=SpeechSinks(on_update=collect, on_interim=ignore, on_lost=lost.append),
    )
    graph = AudioGraph(
        GraphParts(
            activity=CaptureActivity(),
            vad=VadController(VadAnalyzer(vad_params(settings.vad))),
            stt=client,
            on_vad=on_vad,
        )
    )
    queue = PcmQueue()
    await client.start()
    run = asyncio.create_task(graph.run(queue))
    deadline = time.monotonic()
    for offset in range(0, len(pcm), CHUNK_BYTES):
        queue.put(pcm[offset : offset + CHUNK_BYTES])
        deadline += 0.08
        await asyncio.sleep(max(0.0, deadline - time.monotonic()))
    await asyncio.sleep(8)
    run.cancel()
    await client.close()

    assert lost == []
    assert len(finals) == 4
    labels = [update.voice_clusters for update in finals]
    assert all(len(cluster) == 1 for cluster in labels)
    assert labels[0] == labels[2]
    assert labels[1] == labels[3]
    assert labels[0] != labels[1]
    assert speech_starts.count(True) >= 4
    assert speech_starts.count(False) >= 4
