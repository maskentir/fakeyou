# Dokumentasi library

Dibaca pada 2026-09-27 sebelum integrasi ditulis. API juga diperiksa pada source versi terpasang. Lockfile mencatat versi hasil resolusi.

| Library | Sumber | Keputusan |
| --- | --- | --- |
| uv | [Projects](https://docs.astral.sh/uv/guides/projects/) | Python 3.12, src layout, lockfile |
| LangChain | [Agents](https://docs.langchain.com/oss/python/langchain/agents), [Models](https://docs.langchain.com/oss/python/langchain/models) | `create_agent`, `init_chat_model`, injeksi model/tool |
| LangGraph | [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [Memory](https://docs.langchain.com/oss/python/langgraph/add-memory) | `thread_id`, SQLite, PostgreSQL |
| MCP adapters | [Repository](https://github.com/langchain-ai/langchain-mcp-adapters), [MCP](https://docs.langchain.com/oss/python/langchain/mcp) | `load_mcp_tools`, sesi persisten, MCP v1 untuk adapter 0.2.2 |
| Playwright MCP | [Official README](https://github.com/microsoft/playwright-mcp) | CDP, vision, native tools; versi 0.0.82 memakai `browser_click.target` |
| CloakBrowser | [API](https://github.com/CloakHQ/CloakBrowser#api) | `launch_persistent_context_async`, profile per sesi |
| Pipecat | [Muse](https://github.com/pipecat-ai/pipecat/blob/v1.12.0/src/pipecat/services/meta/stt.py), [TTS](https://github.com/pipecat-ai/pipecat/blob/v1.12.0/src/pipecat/services/openai/tts.py) | Native services, tanpa protokol custom |
| Muse | [Speech to text](https://dev.meta.ai/docs/speech-to-text), [Streaming cookbook](https://dev.meta.ai/docs/cookbook/voice-api-fundamentals) | PCM mono, endpointing/diarization, STT saja |
| Graphiti | [Quickstart](https://github.com/getzep/graphiti/tree/main/examples/quickstart), [README](https://github.com/getzep/graphiti) | Native clients, `add_episode`, `search`, scoped group |
| Neo4j | [Docker](https://neo4j.com/docs/operations-manual/current/docker/introduction/) | Community image, volume, autentikasi lokal |
| DeepEval | [Getting started](https://deepeval.com/docs/getting-started), [Tool correctness](https://deepeval.com/docs/metrics-tool-correctness) | Native metric, argument matching; offline judge tidak boleh dipanggil |
| Pydantic Settings | [Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | `.env`, aliases, secrets, validasi |

Dependency tambahan menggantikan komponen yang biasanya ditulis sendiri: Pipecat untuk suara, MCP untuk browser tools, Pydantic Settings untuk konfigurasi, checkpoint resmi untuk persistence. Tidak ada custom agent framework atau database layer.

`memory-init` membuat Graphiti client sebelum memasuki event loop, kemudian memakai metode inisialisasi indeks resmi. `chat --memory` membuat client lewat `asyncio.to_thread` dan menunggu setup indeks sebelum memasang tools. Neo4jDriver 0.30.2 otomatis menjadwalkan inisialisasi ketika constructor dipanggil di dalam loop; memanggil inisialisasi lagi pada saat yang sama menyebabkan race schema yang ditangani upstream, tetapi menghasilkan log error yang membingungkan. Composition ini menghindari inisialisasi ganda tanpa mengakses private API.

Tag database mengikuti [manifest Neo4j resmi](https://github.com/docker-library/official-images/blob/master/library/neo4j) dan [manifest PostgreSQL resmi](https://github.com/docker-library/official-images/blob/master/library/postgres): Neo4j 5.26.31 LTS dan PostgreSQL 17.11.
