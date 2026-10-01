import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cached_property
from typing import Any
from uuid import uuid4

from anthropic import AsyncAnthropic
from anthropic.resources.messages import AsyncMessages
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge, create_entity_edge_embeddings
from graphiti_core.embedder.client import EMBEDDING_DIM
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.errors import GroupsEdgesNotFoundError, NodeNotFoundError
from graphiti_core.graphiti import AddEpisodeResults
from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_client import OpenAIClient
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.nodes import (
    EntityNode,
    EpisodeType,
    EpisodicNode,
    SagaNode,
    create_entity_node_embeddings,
)
from graphiti_core.prompts.models import Message
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters
from langchain.agents.middleware import ToolCallRequest, ToolErrorMiddleware
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel

from fakeyou.faults import FaultReporter
from fakeyou.provider_faults import classify_exception
from fakeyou.settings import (
    DEFAULTS,
    AnthropicMemoryLLM,
    GenericMemoryLLM,
    MemorySettings,
    OpenAIMemoryLLM,
    RecallSettings,
    require_secret,
)

RECALL_RESULT_LIMIT = DEFAULTS.memory.recall.result_limit
RECALL_SEARCH_LIMIT = RECALL_RESULT_LIMIT * 2
RECALL_TIMEOUT_SECONDS = DEFAULTS.memory.recall.timeout_seconds
RECALL_EMPTY_RESULT = "No relevant meeting memory found."
RECALL_UNAVAILABLE_RESULT = "Meeting memory is unavailable right now; treat the answer as unknown."
OWNER_NOTE_SOURCE = "owner note"
FORGET_BATCH_SIZE = 100
REEMBED_BATCH_SIZE = 100
CURRENT_FACTS_FILTER = SearchFilters(
    invalid_at=[[DateFilter(comparison_operator=ComparisonOperator.is_null)]],
    expired_at=[[DateFilter(comparison_operator=ComparisonOperator.is_null)]],
)


def meeting_saga(meeting_id: str) -> str:
    return f"meeting:{meeting_id}"


class GraphitiAnthropicMessages(AsyncMessages):
    async def create(self, *, temperature: float | None = None, **kwargs: Any) -> Any:
        if temperature is not None:
            kwargs["extra_body"] = {**(kwargs.get("extra_body") or {}), "temperature": temperature}
        return await super().create(**kwargs)


class GraphitiAnthropicSDK(AsyncAnthropic):
    @cached_property
    def messages(self) -> GraphitiAnthropicMessages:
        return GraphitiAnthropicMessages(self)


class RelevanceScores(BaseModel):
    scores: list[float]


class AnthropicReranker(CrossEncoderClient):
    def __init__(self, client: AnthropicClient) -> None:
        self.client = client

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        if not passages:
            return []
        response = await self.client.generate_response(
            [
                Message(
                    role="system",
                    content=(
                        "Score each passage's relevance to the query from 0 to 1. "
                        "Return scores in passage order."
                    ),
                ),
                Message(role="user", content=str({"query": query, "passages": passages})),
            ],
            response_model=RelevanceScores,
        )
        scores = RelevanceScores.model_validate(response).scores
        return sorted(zip(passages, scores, strict=True), key=lambda item: item[1], reverse=True)


def create_graphiti_memory(config: MemorySettings) -> Graphiti:
    role = config.llm
    llm_endpoint = role.resolved_base_url
    llm_key = require_secret(role.api_key, "FAKEYOU_MEMORY__LLM__API_KEY")
    embedding_key = require_secret(config.embedder.api_key, "FAKEYOU_MEMORY__EMBEDDER__API_KEY")
    llm_config = LLMConfig(
        api_key=llm_key,
        model=role.model,
        small_model=role.small_model,
        base_url=llm_endpoint,
    )
    llm_client = (
        AnthropicClient(
            config=llm_config,
            client=GraphitiAnthropicSDK(api_key=llm_key, base_url=llm_endpoint),
        )
        if isinstance(role, AnthropicMemoryLLM)
        else {OpenAIMemoryLLM: OpenAIClient, GenericMemoryLLM: OpenAIGenericClient}[type(role)](
            config=llm_config
        )
    )
    embedder = OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            api_key=embedding_key,
            base_url=config.embedder.resolved_base_url,
            embedding_model=config.embedder.model,
            embedding_dim=config.embedder.dim,
        )
    )
    cross_encoder = (
        AnthropicReranker(llm_client)
        if isinstance(llm_client, AnthropicClient)
        else OpenAIRerankerClient(config=llm_config)
    )
    driver = Neo4jDriver(
        uri=config.store.uri,
        user=config.store.user,
        password=require_secret(config.store.password, "FAKEYOU_MEMORY__STORE__PASSWORD"),
        database=config.store.database,
    )
    return Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
    )


@asynccontextmanager
async def open_graphiti_memory(config: MemorySettings) -> AsyncIterator[Graphiti]:
    if config.embedder.dim != EMBEDDING_DIM:
        raise ValueError(
            "memory.embedder.dim must equal EMBEDDING_DIM loaded by Graphiti; "
            "export EMBEDDING_DIM before startup"
        )
    async with AsyncExitStack() as resources:
        memory = await asyncio.to_thread(create_graphiti_memory, config)
        resources.push_async_callback(memory.close)
        yield memory


async def ensure_indexes(memory: Graphiti) -> None:
    await memory.build_indices_and_constraints()


async def verify_memory_connection(memory: Graphiti) -> None:
    if not isinstance(memory.driver, Neo4jDriver):
        raise TypeError("meeting memory requires the Neo4j driver")
    await memory.driver.client.verify_connectivity()


def create_recall_error_middleware(faults: FaultReporter | None) -> ToolErrorMiddleware[Any, Any]:
    def report_unavailable(error: Exception, request: ToolCallRequest) -> str:
        if faults is not None:
            faults.record(classify_exception("memory", error))
        return RECALL_UNAVAILABLE_RESULT

    return ToolErrorMiddleware(report_unavailable, tools=["recall_meeting_context"])


def create_memory_tools(
    memory: Graphiti, *, group_id: str, recall: RecallSettings = DEFAULTS.memory.recall
) -> list[BaseTool]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", group_id):
        raise ValueError("group_id must be 1-128 characters using letters, numbers, '_' or '-'")

    @tool(description="Store one meeting fact in long-term memory.")
    async def remember_meeting_fact(fact: str, source_description: str = "meeting") -> str:
        reference_time = datetime.now(UTC)
        await memory.add_episode(
            name=f"meeting-{reference_time.isoformat()}",
            episode_body=fact,
            source=EpisodeType.text,
            source_description=source_description,
            reference_time=reference_time,
            group_id=group_id,
        )
        return "Remembered meeting fact."

    @tool(description="Search long-term meeting memory for relevant context.")
    async def recall_meeting_context(query: str) -> str:
        async with asyncio.timeout(recall.timeout_seconds):
            results = await memory.search(
                query,
                group_ids=[group_id],
                num_results=recall.result_limit * 2,
                search_filter=CURRENT_FACTS_FILTER,
            )
            first_by_fact: dict[str, EntityEdge] = {}
            for edge in results:
                first_by_fact.setdefault(edge.fact, edge)
            if not first_by_fact:
                return RECALL_EMPTY_RESULT
            return await describe_facts(memory, list(first_by_fact.values())[: recall.result_limit])

    return [remember_meeting_fact, recall_meeting_context]


async def describe_facts(memory: Graphiti, edges: list[EntityEdge]) -> str:
    episode_uuids = sorted({uuid for edge in edges for uuid in edge.episodes})
    episodes = (
        {
            episode.uuid: episode
            for episode in await EpisodicNode.get_by_uuids(memory.driver, episode_uuids)
        }
        if episode_uuids
        else {}
    )
    lines = []
    for edge in edges:
        sources = [episodes[uuid] for uuid in edge.episodes if uuid in episodes]
        if not sources:
            lines.append(f"From an unknown source: {edge.fact}")
            continue
        labels = "; ".join(sorted({source.source_description for source in sources}))
        first_seen = min(source.valid_at for source in sources).astimezone(UTC)
        lines.append(f"From {labels} on {first_seen:%Y-%m-%d %H:%M} UTC: {edge.fact}")
    return "\n".join(lines)


async def remember_owner_note(memory: Graphiti, *, group_id: str, text: str) -> AddEpisodeResults:
    if not text.strip():
        raise ValueError("owner note must not be blank")
    return await memory.add_episode(
        name=f"owner-note:{uuid4()}",
        episode_body=text.strip(),
        source=EpisodeType.text,
        source_description=OWNER_NOTE_SOURCE,
        reference_time=datetime.now(UTC),
        group_id=group_id,
    )


async def meeting_episodes(
    memory: Graphiti, *, group_id: str, meeting_id: str, last_n: int
) -> list[EpisodicNode]:
    return await memory.retrieve_episodes(
        datetime.now(UTC) + timedelta(days=1),
        last_n=last_n,
        group_ids=[group_id],
        saga=meeting_saga(meeting_id),
    )


async def forget_meeting(memory: Graphiti, *, group_id: str, meeting_id: str) -> int:
    saga = meeting_saga(meeting_id)
    removed: set[str] = set()
    while episodes := await meeting_episodes(
        memory, group_id=group_id, meeting_id=meeting_id, last_n=FORGET_BATCH_SIZE
    ):
        for episode in episodes:
            if episode.uuid in removed:
                raise RuntimeError(f"episode {episode.uuid} survived removal")
            await memory.remove_episode(episode.uuid)
            removed.add(episode.uuid)
    for node in await SagaNode.get_by_group_ids(memory.driver, [group_id]):
        if node.name == saga:
            await node.delete(memory.driver)
    return len(removed)


@dataclass(frozen=True, slots=True)
class ReembedResult:
    entities: int
    facts: int


async def reembed_group(memory: Graphiti, *, group_id: str) -> ReembedResult:
    entities = facts = 0
    cursor: str | None = None
    while nodes := await EntityNode.get_by_group_ids(
        memory.driver, [group_id], limit=REEMBED_BATCH_SIZE, uuid_cursor=cursor
    ):
        await create_entity_node_embeddings(memory.embedder, nodes)
        for node in nodes:
            await node.save(memory.driver)
        entities += len(nodes)
        cursor = nodes[-1].uuid
    cursor = None
    while edges := await _entity_edge_page(memory, group_id, cursor):
        await create_entity_edge_embeddings(memory.embedder, edges)
        for edge in edges:
            await edge.save(memory.driver)
        facts += len(edges)
        cursor = edges[-1].uuid
    return ReembedResult(entities, facts)


async def _entity_edge_page(
    memory: Graphiti, group_id: str, cursor: str | None
) -> list[EntityEdge]:
    try:
        edges: list[EntityEdge] = await EntityEdge.get_by_group_ids(
            memory.driver, [group_id], limit=REEMBED_BATCH_SIZE, uuid_cursor=cursor
        )
    except GroupsEdgesNotFoundError:
        return []
    return edges


async def forget_episode(memory: Graphiti, *, group_id: str, episode_uuid: str) -> None:
    try:
        episode = await EpisodicNode.get_by_uuid(memory.driver, episode_uuid)
    except NodeNotFoundError:
        episode = None
    if episode is None or episode.group_id != group_id:
        raise ValueError("episode is not in this persona's memory")
    await memory.remove_episode(episode_uuid)
