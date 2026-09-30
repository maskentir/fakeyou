import inspect

from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.search.search_filters import ComparisonOperator, date_filter_query_constructor
from langchain.agents.middleware import ToolErrorMiddleware
from langchain.chat_models import init_chat_model
from pipecat.frames.frames import TranscriptionFrame
from pipecat.services.meta.stt import META_STT_MODEL, MetaSTTService, MetaSTTSettings

from fakeyou.settings import Settings


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
    assert Settings.model_fields["stt_model"].default == META_STT_MODEL


async def test_meta_stt_drops_diarization_speaker_labels() -> None:
    frames: list[TranscriptionFrame] = []
    stt = MetaSTTService(api_key="key", sample_rate=16000)

    async def collect(frame, direction=None):
        if isinstance(frame, TranscriptionFrame):
            frames.append(frame)

    async def no_metrics(*_args, **_kwargs):
        return None

    stt.push_frame = collect  # type: ignore[method-assign]
    stt.emit_stt_usage_metrics = no_metrics  # type: ignore[method-assign]
    stt._trace_transcription = no_metrics  # type: ignore[method-assign]
    await stt._handle_message({"type": "speaker", "label": "A"})
    await stt._handle_message({"type": "speechComplete", "turnId": 0, "transcript": "Halo."})

    [frame] = frames
    assert frame.text == "Halo."
    assert "speaker" not in frame.result
    assert "label" not in frame.result


def test_tool_error_middleware_filters_by_tool_name() -> None:
    middleware = ToolErrorMiddleware(lambda error, request: "unavailable", tools=["recall"])

    assert middleware.on_error is not None
    assert "tools" in inspect.signature(ToolErrorMiddleware.__init__).parameters


def test_init_chat_model_forwards_openai_compatible_provider_options() -> None:
    model = init_chat_model(
        "openai:muse-spark-1.3",
        api_key="key",
        base_url="https://api.meta.ai/v1",
        reasoning_effort="minimal",
    )

    assert model.openai_api_base == "https://api.meta.ai/v1"
    assert model.reasoning_effort == "minimal"
