from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace
from typing import Any, TypedDict, Unpack, cast, get_args, get_origin, override
from unittest.mock import MagicMock, create_autospec

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.embedder.client import EMBEDDING_DIM, EmbedderClient
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.prompts.models import Message
from neo4j import AsyncDriver
from pydantic import BaseModel

_QUERY_TOKEN = re.compile(r"[a-z0-9]+")
INVALIDATED_AT = datetime(2026, 10, 5, tzinfo=UTC)


def _query_tokens(text: str) -> set[str]:
    return {token for token in _QUERY_TOKEN.findall(text.lower()) if len(token) >= 3}


async def _graphiti_search(
    memory: MagicMock,
    query: str,
    *,
    query_sensitive: bool,
    match_all: bool,
    search_error: BaseException | None,
    **kwargs: Any,
) -> list[Any]:
    memory.searched.append({"query": query, **kwargs})
    memory.search_started.set()
    if memory.search_gate is not None:
        try:
            await memory.search_gate.wait()
        except asyncio.CancelledError:
            memory.search_cancelled.set()
            raise
    if search_error is not None:
        raise search_error
    fallback = (kwargs.get("group_ids") or ["team_alpha"])[0]
    if query_sensitive and not match_all:
        wanted = _query_tokens(query)
        selected = [fact for fact in memory.facts if wanted & _query_tokens(fact)]
    else:
        selected = list(memory.facts)
    if kwargs.get("search_filter") is not None:
        selected = [fact for fact in selected if fact not in memory.invalidated]
    return [
        SimpleNamespace(
            uuid=f"edge-{memory.facts.index(fact)}",
            fact=fact,
            episodes=memory.fact_episodes.get(fact, []),
            group_id=memory.fact_groups.get(fact, fallback),
            invalid_at=INVALIDATED_AT if fact in memory.invalidated else None,
            expired_at=INVALIDATED_AT if fact in memory.invalidated else None,
        )
        for fact in selected
    ]


async def _graphiti_episode_edges(memory: MagicMock, episode_uuids: list[str]) -> Any:
    memory.episode_lookups.append(list(episode_uuids))
    found = [edge for uuid in episode_uuids for edge in memory.episode_edges.get(uuid, [])]
    return SimpleNamespace(edges=found, nodes=[])


async def _graphiti_verify(
    memory: MagicMock,
    *,
    connect_gate: asyncio.Event | None,
    connect_error: BaseException | None,
) -> None:
    memory.events.append("verify")
    if connect_gate is not None:
        await connect_gate.wait()
    if connect_error is not None:
        raise connect_error


async def _graphiti_add_episode(
    memory: MagicMock,
    *,
    episode_prefix: str,
    edges: Sequence[Any],
    **kwargs: Any,
) -> Any:
    if memory.gate is not None:
        await memory.gate.wait()
    attempt = len(memory.added)
    if attempt in memory.errors:
        raise memory.errors.pop(attempt)
    memory.added.append(kwargs)
    uuid = (
        f"uuid-{kwargs['name']}"
        if episode_prefix == "uuid-"
        else f"{episode_prefix}{len(memory.added)}"
    )
    episode = SimpleNamespace(uuid=uuid, name=kwargs["name"])
    memory.saga_episodes.append(episode)
    return SimpleNamespace(episode=episode, edges=list(edges))


async def _graphiti_retrieve_episodes(
    memory: MagicMock,
    reference_time: datetime,
    *,
    saga_episodes: dict[str, list[str]] | None,
    **kwargs: Any,
) -> list[Any]:
    memory.retrieved.append(
        {"reference_time": reference_time, **kwargs} if saga_episodes is not None else kwargs
    )
    if saga_episodes is not None:
        remaining = [
            uuid for uuid in saga_episodes.get(kwargs["saga"], []) if uuid not in memory.removed
        ]
        return [SimpleNamespace(uuid=uuid) for uuid in remaining[: kwargs["last_n"]]]
    return list(memory.saga_episodes[-kwargs["last_n"] :])


async def _graphiti_build(memory: MagicMock, *, fail_build: bool) -> None:
    memory.events.append("build")
    if fail_build:
        raise RuntimeError("index setup failed")


async def _graphiti_close(memory: MagicMock) -> None:
    memory.events.append("close")


async def _graphiti_remove(memory: MagicMock, episode_uuid: str) -> None:
    memory.removed.append(episode_uuid)


async def _graphiti_execute(memory: MagicMock, query: str, **params: Any) -> Any:
    memory.executed.append({"query": query, **params})
    return SimpleNamespace(records=[])


class GraphitiScript(TypedDict, total=False):
    facts: Sequence[str]
    episodes: dict[str, list[str]] | None
    fact_groups: dict[str, str] | None
    query_sensitive: bool
    match_all: bool
    invalidated: Sequence[str]
    existing: Sequence[Any]
    saga_episodes: dict[str, list[str]] | None
    connect_error: BaseException | None
    connect_gate: asyncio.Event | None
    search_error: BaseException | None
    search_gate: asyncio.Event | None
    fail_build: bool
    episode_prefix: str
    edges: Sequence[Any]
    episode_edges: dict[str, list[Any]] | None


def make_graphiti(**script: Unpack[GraphitiScript]) -> MagicMock:
    facts = script.get("facts", ())
    episodes = script.get("episodes")
    fact_groups = script.get("fact_groups")
    query_sensitive = script.get("query_sensitive", False)
    match_all = script.get("match_all", False)
    invalidated = script.get("invalidated", ())
    existing = script.get("existing", ())
    saga_episodes = script.get("saga_episodes")
    connect_error = script.get("connect_error")
    connect_gate = script.get("connect_gate")
    search_error = script.get("search_error")
    search_gate = script.get("search_gate")
    fail_build = script.get("fail_build", False)
    episode_prefix = script.get("episode_prefix", "episode-")
    edges = script.get("edges", ())
    episode_edges = script.get("episode_edges")
    memory = create_autospec(Graphiti, instance=True)
    memory.events = []
    memory.added = []
    memory.searched = []
    memory.retrieved = []
    memory.removed = []
    memory.episode_lookups = []
    memory.executed = []
    memory.episode_edges = dict(episode_edges or {})
    memory.facts = list(facts)
    memory.fact_episodes = episodes or {}
    memory.fact_groups = fact_groups or {}
    memory.saga_episodes = list(existing)
    memory.errors = {}
    memory.gate = None
    memory.search_gate = search_gate
    memory.search_started = asyncio.Event()
    memory.search_cancelled = asyncio.Event()
    memory.driver = create_autospec(Neo4jDriver, instance=True)
    memory.driver.client = create_autospec(AsyncDriver, instance=True)

    memory.build_indices_and_constraints.side_effect = partial(
        _graphiti_build, memory, fail_build=fail_build
    )
    memory.close.side_effect = partial(_graphiti_close, memory)
    memory.invalidated = list(invalidated)
    memory.search.side_effect = partial(
        _graphiti_search,
        memory,
        query_sensitive=query_sensitive,
        match_all=match_all,
        search_error=search_error,
    )
    memory.add_episode.side_effect = partial(
        _graphiti_add_episode, memory, episode_prefix=episode_prefix, edges=edges
    )
    memory.retrieve_episodes.side_effect = partial(
        _graphiti_retrieve_episodes, memory, saga_episodes=saga_episodes
    )
    memory.remove_episode.side_effect = partial(_graphiti_remove, memory)
    memory.get_nodes_and_edges_by_episode.side_effect = partial(_graphiti_episode_edges, memory)
    memory.driver.execute_query.side_effect = partial(_graphiti_execute, memory)
    memory.driver.client.verify_connectivity.side_effect = partial(
        _graphiti_verify, memory, connect_gate=connect_gate, connect_error=connect_error
    )
    return cast(MagicMock, memory)


class FakeSaga:
    def __init__(self, name: str, deleted: list[str]) -> None:
        self.name = name
        self.deleted = deleted

    async def delete(self, driver: Any) -> None:
        del driver
        self.deleted.append(self.name)


def _filled_value(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin in (list, set, frozenset):
        return [] if origin is list else origin()
    if origin is dict:
        return {}
    args = [item for item in get_args(annotation) if item is not type(None)]
    if len(args) < len(get_args(annotation)):
        return None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {
            name: _filled_value(field.annotation) for name, field in annotation.model_fields.items()
        }
    if annotation is str:
        return ""
    if annotation is bool:
        return False
    if annotation in (int, float):
        return annotation()
    return ""


class FakeGraphitiLLM(LLMClient):
    def __init__(self) -> None:
        super().__init__(None)
        self.calls: list[str] = []

    @override
    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = 16384,
        model_size: Any = None,
    ) -> dict[str, Any]:
        del messages, max_tokens, model_size
        if response_model is None:
            self.calls.append("none")
            return {}
        self.calls.append(response_model.__name__)
        return {
            name: _filled_value(field.annotation)
            for name, field in response_model.model_fields.items()
        }


class FakeEmbedder(EmbedderClient):
    @override
    async def create(self, input_data: Any) -> list[float]:
        text = input_data if isinstance(input_data, str) else " ".join(map(str, input_data))
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[index % len(digest)] / 255.0 for index in range(EMBEDDING_DIM)]


class FakeCrossEncoder(CrossEncoderClient):
    @override
    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        wanted = _query_tokens(query)
        scored = [
            (passage, len(wanted & _query_tokens(passage)) / max(len(wanted), 1))
            for passage in passages
        ]
        return sorted(scored, key=lambda item: item[1], reverse=True)


def make_real_graphiti(uri: str, *, password: str) -> Graphiti:
    driver = Neo4jDriver(uri=uri, user="neo4j", password=password, database="neo4j")
    return Graphiti(
        graph_driver=driver,
        llm_client=FakeGraphitiLLM(),
        embedder=FakeEmbedder(),
        cross_encoder=FakeCrossEncoder(),
    )
