import asyncio
import socket
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from graphiti_core.nodes import EpisodeType
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from pipecat.frames.frames import (
    EndFrame,
    ErrorFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStoppedFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from fakeyou.agent import create_meeting_agent
from fakeyou.memory import Neo4jMemoryConfig, create_graphiti_memory
from fakeyou.settings import Settings, require_secret
from fakeyou.voice import create_openai_tts


def _secret_or_skip(settings_value, name: str) -> str:
    try:
        return require_secret(settings_value, name)
    except ValueError as error:
        pytest.skip(str(error))


def _fail_provider(error: Exception, provider: str) -> None:
    status = getattr(error, "status_code", None)
    detail = f" ({status})" if isinstance(status, int) else ""
    pytest.fail(f"{provider} failed: {type(error).__name__}{detail}", pytrace=False)


class _AudioCollector(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.audio_bytes = 0
        self.audio_frames = 0
        self.stopped = asyncio.Event()
        self.errors: list[str] = []

    async def process_frame(self, frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            self.audio_bytes += len(frame.audio)
            self.audio_frames += 1
        elif isinstance(frame, TTSStoppedFrame):
            self.stopped.set()
        elif isinstance(frame, ErrorFrame):
            self.errors.append(frame.error)
        await self.push_frame(frame, direction)


@pytest.mark.live
async def test_native_agent_returns_openai_response_and_executes_tool() -> None:
    settings = Settings()
    api_key = _secret_or_skip(settings.openai_api_key, "FAKEYOU_OPENAI_API_KEY")

    @tool(description="Return the synthetic agenda marker for a meeting test.")
    def read_agenda_marker() -> str:
        return "agenda-marker-731"

    try:
        model = init_chat_model(settings.chat_model, api_key=api_key, timeout=30, max_retries=0)
        agent = create_meeting_agent(model, tools=[read_agenda_marker])
        async with asyncio.timeout(90):
            result = await agent.ainvoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Call read_agenda_marker and include its exact value "
                                "in your answer."
                            ),
                        }
                    ]
                },
                config={"recursion_limit": 8},
            )
    except Exception as error:
        _fail_provider(error, "OpenAI agent")

    messages = result["messages"]
    assert any(
        isinstance(message, ToolMessage) and message.content == "agenda-marker-731"
        for message in messages
    )
    assert isinstance(messages[-1], AIMessage)
    assert "agenda-marker-731" in str(messages[-1].content)


@pytest.mark.live
async def test_native_openai_tts_streams_audio_frames() -> None:
    settings = Settings()
    api_key = _secret_or_skip(settings.openai_api_key, "FAKEYOU_OPENAI_API_KEY")
    collector = _AudioCollector()
    tts = create_openai_tts(api_key=api_key)
    worker = PipelineWorker(
        Pipeline([tts, collector]),
        params=PipelineParams(audio_out_sample_rate=24_000),
        idle_timeout_secs=45,
    )
    started = asyncio.Event()
    errors: list[str] = []

    @worker.event_handler("on_pipeline_started")
    async def on_pipeline_started(_, __) -> None:
        started.set()

    @worker.event_handler("on_pipeline_error")
    async def on_pipeline_error(_, frame: ErrorFrame) -> None:
        errors.append(frame.error)

    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
    await runner.add_workers(worker)
    runner_task = asyncio.create_task(runner.run())
    try:
        async with asyncio.timeout(60):
            await started.wait()
            await worker.queue_frame(TTSSpeakFrame("Hello from the test meeting."))
            await collector.stopped.wait()
            await worker.queue_frame(EndFrame(reason="live TTS check complete"))
            await runner_task
    except Exception as error:
        _fail_provider(error, "OpenAI TTS")
    finally:
        if not runner_task.done():
            await worker.cancel(reason="live TTS check cleanup")
            await asyncio.wait_for(runner_task, timeout=15)

    assert not errors and not collector.errors, "OpenAI TTS emitted a pipeline error"
    assert collector.audio_frames > 0
    assert collector.audio_bytes > 0


@pytest.mark.live
async def test_graphiti_fact_survives_client_reopen() -> None:
    settings = Settings()
    api_key = _secret_or_skip(settings.openai_api_key, "FAKEYOU_OPENAI_API_KEY")
    parsed = urlparse(settings.neo4j_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 7687
    try:
        with socket.create_connection((host, port), timeout=2):
            pass
    except OSError:
        pytest.skip("Neo4j is not listening at the configured URI")

    group_id = f"live_provider_{uuid4().hex}"
    marker = f"orchid-{uuid4().hex[:12]}"
    fact = f"Synthetic attendee {marker} prefers jasmine tea."
    config = Neo4jMemoryConfig(
        uri=settings.neo4j_uri,
        user=settings.neo4j_user,
        password=_secret_or_skip(settings.neo4j_password, "FAKEYOU_NEO4J_PASSWORD"),
        api_key=api_key,
    )
    episode_uuid: str | None = None
    memory = None
    try:
        async with asyncio.timeout(240):
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            await memory.build_indices_and_constraints()
            result = await memory.add_episode(
                name=f"provider-check-{marker}",
                episode_body=fact,
                source=EpisodeType.text,
                source_description="synthetic live provider check",
                reference_time=datetime.now(UTC),
                group_id=group_id,
            )
            episode_uuid = result.episode.uuid
            await memory.close()
            memory = None
            memory = await asyncio.to_thread(create_graphiti_memory, config)
            matches = await memory.search(f"{marker} jasmine tea", group_ids=[group_id])
            assert any(marker in edge.fact for edge in matches)
    except Exception as error:
        _fail_provider(error, "Graphiti add/reopen/search")
    finally:
        if episode_uuid is not None and memory is None:
            memory = await asyncio.to_thread(create_graphiti_memory, config)
        if memory is not None:
            try:
                if episode_uuid is not None:
                    async with asyncio.timeout(30):
                        await memory.remove_episode(episode_uuid)
            finally:
                await memory.close()
