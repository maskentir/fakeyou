# Voice foundation

The voice module exposes native Pipecat services only. `create_muse_stt` creates `MetaSTTService` for Muse Voice Transcribe, and `create_openai_tts` creates `OpenAITTSService` for speech output. The Muse service defaults to Indonesian language bias and model endpointing; callers can pass a language, multiple language biases, keywords, or `PUSH_TO_TALK` / `DIARIZATION` mode.

Pipecat's released `1.12.0` implementation is the supported Muse adapter. Install it with `pipecat-ai[meta]`; the extra is empty in that release because the adapter uses Pipecat's core WebSocket dependencies. OpenAI TTS is included in the core dependency set; `pipecat-ai[openai]` is an empty compatibility extra.

The documented voice pipeline has four ports: transport input produces audio frames, STT produces interim and final transcription frames, the LLM context aggregator produces user messages and accepts assistant messages, and transport output consumes TTS audio frames. Pipecat's official `voice-openai.py` example wires these processors in this order: `transport.input()`, STT, user aggregator, LLM, TTS, `transport.output()`, and assistant aggregator. The factories in this project provide only the native STT and TTS processors; a future transport can be inserted at the two transport ports.

This composition is a foundation snippet, not a Google Meet media bridge. `transport.input()` and `transport.output()` still need a real audio transport. CloakBrowser and Playwright operate the Meet page and browser UI; they do not supply Pipecat transport objects or route Meet WebRTC media into this pipeline.

Muse `ENDPOINTING` emits model-detected speech boundaries and final transcript frames. Pipecat still uses its own VAD and turn strategy to decide when the user turn is complete. `PUSH_TO_TALK` leaves turn delimiting to the client. `DIARIZATION` adds session-scoped speaker attribution; it does not provide verified participant identities. VAD and interruption policy remain pipeline concerns, and TTS cancellation on barge-in depends on the transport and pipeline lifecycle.

In Pipecat 1.12.0, the Muse adapter logs `speaker`, `speechStart`, and `speechEnd` events internally. It does not map speaker events to participant identities on transcription frames. A downstream pipeline cannot treat the current transcription `user_id` as verified Muse diarization. The [live smoke test](live-muse-test.md) checks final transcription in `ENDPOINTING` mode.

The remaining integration seam is audio routing: capture Meet's remote audio as mono PCM into the transport input, and route Pipecat's TTS output to the browser microphone. That seam requires a separately verified virtual audio or WebRTC bridge and is intentionally outside this foundation.

Pipecat 1.12.0 also includes `LangchainProcessor` in `pipecat.processors.frameworks.langchain`. It accepts a LangChain `Runnable`, extracts only the latest plain-text message from an `LLMContextFrame`, and streams `AIMessageChunk` or string output as `TextFrame` objects. It is a lightweight text-in/text-out bridge: it does not pass the full message list, tool calls, or multimodal content to the runnable. It also requires the separate `pipecat-ai[langchain]` extra, which is not part of this foundation's dependency set. For the existing LangGraph/LangChain agent, keep orchestration outside the voice pipeline until a dedicated adapter owns the message and tool contract. Pipecat's native LLM service path is the compatible option for a complete conversational pipeline.

References:

- [Pipecat 1.12.0 Muse source](https://raw.githubusercontent.com/pipecat-ai/pipecat/v1.12.0/src/pipecat/services/meta/stt.py)
- [Pipecat 1.12.0 package metadata](https://raw.githubusercontent.com/pipecat-ai/pipecat/v1.12.0/pyproject.toml)
- [Pipecat voice pipeline example](https://github.com/pipecat-ai/pipecat/blob/main/examples/voice/voice-openai.py)
- [Pipecat 1.12.0 LangchainProcessor](https://raw.githubusercontent.com/pipecat-ai/pipecat/v1.12.0/src/pipecat/processors/frameworks/langchain.py)
- [Meta Muse Voice Transcribe API](https://dev.meta.ai/docs/speech-to-text)
- [Meta streaming cookbook](https://dev.meta.ai/docs/cookbook/voice-api-fundamentals)
