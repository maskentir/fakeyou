import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge, create_entity_edge_embeddings
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.errors import GroupsEdgesNotFoundError, NodeNotFoundError
from graphiti_core.graphiti import AddEpisodeResults
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
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters
from langchain.agents.middleware import ToolCallRequest, ToolErrorMiddleware
from langchain_core.tools import BaseTool, tool

from fakeyou.faults import FaultReporter
from fakeyou.provider_faults import classify_exception

RECALL_RESULT_LIMIT = 5
RECALL_SEARCH_LIMIT = RECALL_RESULT_LIMIT * 2
RECALL_TIMEOUT_SECONDS = 15.0
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


@dataclass(frozen=True, slots=True)
class Neo4jMemoryConfig:
    uri: str
    user: str
    password: str = field(repr=False)
    api_key: str = field(repr=False)
    llm_model: str
    small_model: str
    embedding_model: str
    database: str = "neo4j"
    embedding_base_url: str | None = None
    embedding_api_key: str | None = field(default=None, repr=False)
    embedding_dim: int = 1024
    llm_base_url: str | None = None
    llm_api_key: str | None = field(default=None, repr=False)


def create_graphiti_memory(config: Neo4jMemoryConfig) -> Graphiti:
    llm_config = LLMConfig(
        api_key=config.llm_api_key or config.api_key,
        model=config.llm_model,
        small_model=config.small_model,
        base_url=config.llm_base_url,
    )
    llm_client = (
        OpenAIGenericClient(config=llm_config)
        if config.llm_base_url
        else OpenAIClient(config=llm_config)
    )
    embedder = OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            api_key=config.embedding_api_key or config.api_key,
            base_url=config.embedding_base_url,
            embedding_model=config.embedding_model,
            embedding_dim=config.embedding_dim,
        )
    )
    cross_encoder = OpenAIRerankerClient(config=llm_config)
    driver = Neo4jDriver(
        uri=config.uri,
        user=config.user,
        password=config.password,
        database=config.database,
    )
    return Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=embedder,
        cross_encoder=cross_encoder,
    )


@asynccontextmanager
async def open_graphiti_memory(config: Neo4jMemoryConfig) -> AsyncIterator[Graphiti]:
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


def recall_error_middleware(faults: FaultReporter | None) -> ToolErrorMiddleware[Any, Any]:
    def report_unavailable(error: Exception, request: ToolCallRequest) -> str:
        if faults is not None:
            faults.record(classify_exception("memory", error))
        return RECALL_UNAVAILABLE_RESULT

    return ToolErrorMiddleware(report_unavailable, tools=["recall_meeting_context"])


def create_memory_tools(memory: Graphiti, *, group_id: str) -> list[BaseTool]:
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
        async with asyncio.timeout(RECALL_TIMEOUT_SECONDS):
            results = await memory.search(
                query,
                group_ids=[group_id],
                num_results=RECALL_SEARCH_LIMIT,
                search_filter=CURRENT_FACTS_FILTER,
            )
            first_by_fact: dict[str, EntityEdge] = {}
            for edge in results:
                first_by_fact.setdefault(edge.fact, edge)
            if not first_by_fact:
                return RECALL_EMPTY_RESULT
            return await describe_facts(memory, list(first_by_fact.values())[:RECALL_RESULT_LIMIT])

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
