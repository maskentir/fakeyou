import asyncio
import inspect
import json
from typing import Any, override

import httpx2
import pytest
from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EpisodicNode
from graphiti_core.search.search_filters import ComparisonOperator, date_filter_query_constructor
from langchain.agents.middleware import ToolErrorMiddleware
from langchain.chat_models import init_chat_model
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from neo4j.exceptions import ConstraintError
from neo4j.time import DateTime as Neo4jDateTime
from pipecat.frames.frames import EndFrame, Frame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.meta.stt import META_STT_MODEL, MetaSTTService, MetaSTTSettings
from pipecat.workers.runner import WorkerRunner
from websockets.asyncio.server import ServerConnection, serve

from fakeyou.config.settings import STTSettings


def test_graphiti_add_episode_accepts_the_arguments_ingestion_relies_on() -> None:
    parameters = inspect.signature(Graphiti.add_episode).parameters

    for name in ("saga", "saga_previous_episode_uuid", "custom_extraction_instructions", "source"):
        assert name in parameters


def test_graphiti_retrieves_episodes_by_saga() -> None:
    assert "saga" in inspect.signature(Graphiti.retrieve_episodes).parameters


def test_graphiti_node_not_found_is_not_a_value_error() -> None:
    assert not issubclass(NodeNotFoundError, ValueError)


def test_graphiti_is_null_filter_renders_without_a_parameter() -> None:
    query = date_filter_query_constructor(
        "e.invalid_at", "$invalid_at_0", ComparisonOperator.is_null
    )

    assert query == "(e.invalid_at IS NULL)"


def test_neo4j_driver_keeps_group_ids_in_one_database() -> None:
    driver = Neo4jDriver("bolt://127.0.0.1:9", "neo4j", "password")

    assert driver.clone(database="twin_group") is driver


def test_meta_stt_exposes_keywords_language_bias_and_mode() -> None:
    fields = MetaSTTSettings.__dataclass_fields__

    for name in ("keywords", "language_bias", "mode"):
        assert name in fields
    assert STTSettings.model_fields["model"].default == META_STT_MODEL


class TranscriptCollector(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.finals: list[TranscriptionFrame] = []
        self.received = asyncio.Event()

    @override
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            self.finals.append(frame)
            self.received.set()
        await self.push_frame(frame, direction)


async def test_meta_stt_drops_diarization_speaker_labels() -> None:
    async def meta_server(connection: ServerConnection) -> None:
        await connection.recv()
        await connection.send(json.dumps({"sessionId": "s1"}))
        await connection.send(json.dumps({"type": "speaker", "label": "A"}))
        await connection.send(
            json.dumps({"type": "speechComplete", "turnId": 0, "transcript": "Hello."})
        )
        await connection.wait_closed()

    async with serve(meta_server, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        stt = MetaSTTService(api_key="key", url=f"ws://127.0.0.1:{port}", sample_rate=16000)
        collector = TranscriptCollector()
        worker = PipelineWorker(Pipeline([stt, collector]), cancel_on_idle_timeout=False)

        async def end_after_transcript() -> None:
            await asyncio.wait_for(collector.received.wait(), timeout=5)
            await worker.queue_frame(EndFrame())

        runner = WorkerRunner()
        await runner.add_workers(worker)
        await asyncio.gather(runner.run(), end_after_transcript())

    [frame] = collector.finals
    assert frame.text == "Hello."
    assert frame.result is not None
    assert "speaker" not in frame.result
    assert "label" not in frame.result


def test_tool_error_middleware_filters_by_tool_name() -> None:
    middleware = ToolErrorMiddleware(lambda _error, _request: "unavailable", tools=["recall"])

    assert middleware.on_error is not None
    assert "tools" in inspect.signature(ToolErrorMiddleware.__init__).parameters


def test_init_chat_model_forwards_openai_compatible_provider_options() -> None:
    model = init_chat_model(
        "muse-spark-1.3",
        model_provider="openai",
        api_key="key",
        base_url="https://api.meta.ai/v1",
        reasoning_effort="minimal",
    )

    assert isinstance(model, ChatOpenAI)
    assert model.openai_api_base == "https://api.meta.ai/v1"
    assert model.reasoning_effort == "minimal"


def test_init_chat_model_forwards_google_reasoning_effort() -> None:
    model = init_chat_model(
        "gemini-3.8-flash",
        model_provider="google_genai",
        api_key="key",
        reasoning_effort="low",
    )

    assert isinstance(model, ChatGoogleGenerativeAI)
    assert model.reasoning_effort == "low"


def test_init_chat_model_sends_extra_body_fields_in_the_request() -> None:
    sent: list[dict[str, Any]] = []

    def provider(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "completion-1",
                "object": "chat.completion",
                "created": 0,
                "model": "MiniMax-M2.7-highspeed",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    with httpx2.Client(transport=httpx2.MockTransport(provider)) as http_client:
        model = init_chat_model(
            "openai:MiniMax-M2.7-highspeed",
            api_key="key",
            base_url="https://api.minimax.io/v1",
            extra_body={"reasoning_split": True},
            http_client=http_client,
        )
        assert isinstance(model, ChatOpenAI)
        model.invoke("hello")

    [body] = sent
    assert body["reasoning_split"] is True
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_init_chat_model_builds_claude_with_the_reply_timeout_and_retries() -> None:
    from langchain_anthropic import ChatAnthropic

    model = init_chat_model(
        "anthropic:claude-haiku-4-5-20251001", api_key="key", timeout=20, max_retries=0
    )

    assert isinstance(model, ChatAnthropic)
    assert (model.model, model.default_request_timeout, model.max_retries) == (
        "claude-haiku-4-5-20251001",
        20,
        0,
    )
    assert model.anthropic_api_key.get_secret_value() == "key"


def test_env_settings_source_exposes_mutable_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_settings.sources import EnvSettingsSource

    from fakeyou.config.settings import Settings

    monkeypatch.setenv("FAKEYOU_REPLY__MODEL", "contract-model")
    source = EnvSettingsSource(Settings, env_prefix="FAKEYOU_", env_nested_delimiter="__")

    assert source.env_vars["fakeyou_reply__model"] == "contract-model"
    source.env_vars = {}
    assert source.env_vars == {}


async def test_checkpointer_reports_whether_a_thread_has_a_checkpoint() -> None:
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from fakeyou.db.checkpoint import open_checkpointer
    from tests._fakes_common import DATABASE_URL

    async with open_checkpointer(DATABASE_URL) as saver:
        assert await saver.aget_tuple({"configurable": {"thread_id": "meeting:a"}}) is None
        agent = create_agent(
            FakeMessagesListChatModel(responses=[AIMessage(content="ok")]), checkpointer=saver
        )
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": "hello"}]},
            {"configurable": {"thread_id": "meeting:a"}},
        )

        assert await saver.aget_tuple({"configurable": {"thread_id": "meeting:a"}}) is not None
        assert await saver.aget_tuple({"configurable": {"thread_id": "meeting:b"}}) is None


def test_owner_note_memory_contracts_cover_correction_path() -> None:
    assert callable(EpisodicNode.save)
    assert callable(EpisodicNode.get_by_uuid)
    assert callable(EpisodicNode.get_by_uuids)
    assert callable(EntityEdge.get_by_uuids)
    assert callable(Neo4jDriver.transaction)
    assert callable(Neo4jDriver.execute_query)
    assert issubclass(ConstraintError, Exception)
    assert callable(Neo4jDateTime.from_native)
    assert callable(Neo4jDateTime.to_native)
