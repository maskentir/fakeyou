import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_client import OpenAIClient
from graphiti_core.nodes import EpisodeType
from langchain_core.tools import BaseTool, tool

RECALL_RESULT_LIMIT = 5
RECALL_EMPTY_RESULT = "No relevant meeting memory found."


@dataclass(frozen=True, slots=True)
class Neo4jMemoryConfig:
    uri: str
    user: str
    password: str = field(repr=False)
    api_key: str = field(repr=False)
    database: str = "neo4j"


def create_graphiti_memory(config: Neo4jMemoryConfig) -> Graphiti:
    llm_config = LLMConfig(api_key=config.api_key)
    llm_client = OpenAIClient(config=llm_config)
    embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(api_key=config.api_key))
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
        results = await memory.search(query, group_ids=[group_id], num_results=RECALL_RESULT_LIMIT)
        facts = [result.fact for result in results[:RECALL_RESULT_LIMIT]]
        if not facts:
            return RECALL_EMPTY_RESULT
        return "\n".join(facts)

    return [remember_meeting_fact, recall_meeting_context]
