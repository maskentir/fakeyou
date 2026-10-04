import inspect

from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EpisodicNode
from graphiti_core.search.search_filters import ComparisonOperator, date_filter_query_constructor
from langchain.agents.middleware import ToolErrorMiddleware
from langchain.chat_models import init_chat_model
from neo4j.exceptions import ConstraintError
from neo4j.time import DateTime as Neo4jDateTime
from pipecat.frames.frames import TranscriptionFrame
from pipecat.services.meta.stt import META_STT_MODEL, MetaSTTService, MetaSTTSettings

from fakeyou.settings import STTSettings


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


async def test_meta_stt_drops_diarization_speaker_labels() -> None:
    frames: list[TranscriptionFrame] = []
    stt = MetaSTTService(api_key="key", sample_rate=16000)

    async def collect(frame, direction=None):
        if isinstance(frame, TranscriptionFrame):
            frames.append(frame)

    async def no_metrics(*_args, **_kwargs):
        return None

    stt.push_frame = collect
    stt.emit_stt_usage_metrics = no_metrics
    stt._trace_transcription = no_metrics
    await stt._handle_message({"type": "speaker", "label": "A"})
    await stt._handle_message({"type": "speechComplete", "turnId": 0, "transcript": "Hello."})

    [frame] = frames
    assert frame.text == "Hello."
    assert "speaker" not in frame.result
    assert "label" not in frame.result


def test_tool_error_middleware_filters_by_tool_name() -> None:
    middleware = ToolErrorMiddleware(lambda error, request: "unavailable", tools=["recall"])

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

    assert model.openai_api_base == "https://api.meta.ai/v1"
    assert model.reasoning_effort == "minimal"


def test_init_chat_model_forwards_google_reasoning_effort() -> None:
    model = init_chat_model(
        "gemini-3.8-flash",
        model_provider="google_genai",
        api_key="key",
        reasoning_effort="low",
    )

    assert model.reasoning_effort == "low"


def test_init_chat_model_sends_extra_body_fields_in_the_request() -> None:
    model = init_chat_model(
        "openai:MiniMax-M2.7-highspeed",
        api_key="key",
        base_url="https://api.minimax.io/v1",
        extra_body={"reasoning_split": True},
    )

    payload = model._get_request_payload([("user", "hello")])

    assert payload["extra_body"] == {"reasoning_split": True}


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


def test_env_settings_source_exposes_mutable_env_vars(monkeypatch) -> None:
    from pydantic_settings.sources import EnvSettingsSource

    from fakeyou.settings import Settings

    monkeypatch.setenv("FAKEYOU_REPLY__MODEL", "contract-model")
    source = EnvSettingsSource(Settings, env_prefix="FAKEYOU_", env_nested_delimiter="__")

    assert source.env_vars["fakeyou_reply__model"] == "contract-model"
    source.env_vars = {}
    assert source.env_vars == {}


async def test_sqlite_saver_reports_whether_a_thread_has_a_checkpoint(tmp_path) -> None:
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite")) as saver:
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
