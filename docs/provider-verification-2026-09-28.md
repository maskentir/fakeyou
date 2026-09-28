# Live provider verification — 2026-09-28

The opt-in checks in `tests/test_providers_live.py` exercise the native library factories with the configured `.env` credentials. They use synthetic text and a short generated speech sample. Run them from the repository root with:

```sh
uv run --no-sync pytest -m live tests/test_providers_live.py -q --tb=short
```

Result on 2026-09-28: **3 passed, 3 warnings in 20.84s**.

| Check | Observed result |
| --- | --- |
| LangChain agent and OpenAI | `create_meeting_agent` returned an OpenAI assistant message containing the exact marker returned by a harmless local tool. The message history included the corresponding `ToolMessage`. |
| Pipecat and OpenAI TTS | `create_openai_tts` ran inside a `PipelineWorker` and `WorkerRunner`. The collector received at least one nonempty `TTSAudioRawFrame` and a `TTSStoppedFrame`; the worker shut down. |
| Graphiti and Neo4j | `create_graphiti_memory` added one synthetic episode in a unique `live_provider_...` group. After closing and reopening the client, scoped search found its marker in an edge fact. The test removed the episode by its returned UUID and closed the client. |

The checks use bounded timeouts. Missing API credentials or an unavailable Neo4j listener skip the affected test. Once a provider call starts, authentication, billing, API, and database errors fail the check with a concise exception type and status where available. The tests do not log credentials or full provider responses.

These results verify one live request per path and do not establish Google Meet access, meeting audio quality, sustained latency, or production reliability. The three warnings came from installed dependencies: Graphiti's Pydantic class-based config, Pipecat's `audioop` import, and Pipecat's deprecated `AudioContextTTSService` class. None failed the checks.

API references read before implementation: [LangChain agents](https://docs.langchain.com/oss/python/langchain/agents), [Pipecat OpenAI TTS](https://docs.pipecat.ai/api-reference/server/services/tts/openai), [Graphiti episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes), and [Graphiti namespacing](https://help.getzep.com/v2/graphiti/core-concepts/graph-namespacing). Installed package signatures and the Pipecat TTS and Graphiti cleanup implementations were also inspected for the project's resolved versions.
