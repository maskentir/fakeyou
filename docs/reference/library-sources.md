# Dokumentasi library

Dibaca pada 2026-09-27 sebelum integrasi ditulis. API juga diperiksa pada source versi terpasang. Lockfile mencatat versi hasil resolusi. Baris Silero VAD, loudness, sentencex, onnxruntime, dan websockets ditambahkan pada 2026-10-15 dari metadata paket yang terkunci.

| Library | Sumber | Keputusan |
| --- | --- | --- |
| uv | [Projects](https://docs.astral.sh/uv/guides/projects/) | Python 3.12, src layout, lockfile |
| LangChain | [Agents](https://docs.langchain.com/oss/python/langchain/agents), [Models](https://docs.langchain.com/oss/python/langchain/models) | `create_agent`, `init_chat_model`, injeksi model/tool |
| LangGraph | [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence), [Memory](https://docs.langchain.com/oss/python/langgraph/add-memory) | `thread_id`, SQLite, PostgreSQL |
| MCP adapters | [Repository](https://github.com/langchain-ai/langchain-mcp-adapters), [MCP](https://docs.langchain.com/oss/python/langchain/mcp) | `load_mcp_tools`, sesi persisten, MCP v1 untuk adapter 0.2.2 |
| Playwright MCP | [Official README](https://github.com/microsoft/playwright-mcp) | CDP, vision, native tools; versi 0.0.82 memakai `browser_click.target` |
| CloakBrowser | [API](https://github.com/CloakHQ/CloakBrowser#api) | `launch_persistent_context_async`, profile per sesi |
| Silero VAD | [Repository](https://github.com/snakers4/silero-vad) | Lisensi MIT. Model v5 ONNX di-vendor di `src/fakeyou/audio/models/`; sha256 dan sumber di [README model](../../src/fakeyou/audio/models/README.md); dipakai `audio/vad.py` |
| loudness | [Repository](https://github.com/iver56/loudness) | Lisensi MIT. Volume BS.1770 (ITU-R BS.1770) untuk VAD; `>=0.2,<1`, terkunci 0.2.0 |
| sentencex | [Repository](https://github.com/wikimedia/sentencex) | Lisensi MIT. Batas kalimat di `foundation/sentences.py`; `==1.0.31` |
| onnxruntime | [Situs](https://onnxruntime.ai) | Lisensi MIT. Inferensi model Silero VAD; `~=1.24.3`, terkunci 1.24.4 |
| websockets | [Repository](https://github.com/python-websockets/websockets), [Dokumentasi](https://websockets.readthedocs.io/) | Lisensi BSD-3-Clause. Socket klien Muse di `transcription/muse.py`; `>=16,<17`, terkunci 16.1.1 |
| Meta Muse | [Speech to text](https://dev.meta.ai/docs/speech-to-text), [Streaming cookbook](https://dev.meta.ai/docs/cookbook/voice-api-fundamentals) | PCM mono 16 kHz; mode `DIARIZATION` (default) atau `ENDPOINTING`; STT saja; klien sendiri di `transcription/muse.py` |
| Graphiti | [Quickstart](https://github.com/getzep/graphiti/tree/main/examples/quickstart), [README](https://github.com/getzep/graphiti) | Native clients, `add_episode`, `search`, scoped group |
| Neo4j | [Docker](https://neo4j.com/docs/operations-manual/current/docker/introduction/) | Community image, volume, autentikasi lokal |
| DeepEval | [Getting started](https://deepeval.com/docs/getting-started), [Tool correctness](https://deepeval.com/docs/metrics-tool-correctness) | Native metric, argument matching; offline judge tidak boleh dipanggil |
| Pydantic Settings | [Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | `.env`, aliases, secrets, validasi |

Dependency tambahan menggantikan komponen yang biasanya ditulis sendiri: MCP untuk browser tools, Pydantic Settings untuk konfigurasi, checkpoint resmi untuk persistence. Tidak ada custom agent framework atau database layer.

`memory-init` membuat Graphiti client sebelum memasuki event loop, kemudian memakai metode inisialisasi indeks resmi. `chat --memory` membuat client lewat `asyncio.to_thread` dan menunggu setup indeks sebelum memasang tools. Neo4jDriver 0.30.2 otomatis menjadwalkan inisialisasi ketika constructor dipanggil di dalam loop; memanggil inisialisasi lagi pada saat yang sama menyebabkan race schema yang ditangani upstream, tetapi menghasilkan log error yang membingungkan. Composition ini menghindari inisialisasi ganda tanpa mengakses private API.

Tag database mengikuti [manifest Neo4j resmi](https://github.com/docker-library/official-images/blob/master/library/neo4j) dan [manifest PostgreSQL resmi](https://github.com/docker-library/official-images/blob/master/library/postgres): Neo4j 5.26.31 LTS dan PostgreSQL 17.11.

## Checklist upgrade versi terkunci

Perilaku library yang diandalkan dikunci oleh `tests/test_library_contracts.py`. Sebelum menaikkan versi Graphiti, LangChain, atau langchain-openai:

1. Baca changelog dan source versi baru untuk API di tabel di atas; dokumentasi web bisa berbeda dari source (contoh: `add_episode_bulk` di Graphiti 0.30.2).
2. Jalankan `uv run pytest tests/test_library_contracts.py`. Tes yang gagal menandai asumsi yang berubah, misalnya `Neo4jDriver.clone` mulai memisahkan database per grup.
3. Bandingkan default model library dengan setelan eksplisit di `Settings` (`FAKEYOU_MEMORY__LLM__*`, `FAKEYOU_MEMORY__EMBEDDER__*`, `FAKEYOU_STT__MODEL`). Proyek tidak boleh bergantung pada default library.
4. Jalankan suite offline, tes live provider, eval persona live, dan tes image worker (`-m container`) sebelum merge.

