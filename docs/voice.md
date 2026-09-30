# Voice foundation

The voice module uses the native Pipecat adapter for capture. `create_muse_stt` creates `MetaSTTService` for Muse Voice Transcribe, and defaults to Indonesian language bias and model endpointing; callers can pass a language, multiple language biases, keywords, or `PUSH_TO_TALK` / `DIARIZATION` mode. Speech output uses `OpenAISpeechSynthesizer` and the native OpenAI streaming response API to yield raw PCM. Its buffered method remains available for explicit callers and compatibility with synthesizers that lack streaming support.

Pipecat's released `1.12.0` implementation is the supported Muse adapter. Install it with `pipecat-ai[meta]`; the extra is empty in that release because the adapter uses Pipecat's core WebSocket dependencies. Synthesis uses the OpenAI client directly, outside the Pipecat pipeline.

The voice pipeline is capture-only: `transport.input()`, STT, then a transcript observer that forwards final transcription frames to the meeting session. Pipecat's official `voice-openai.py` example wires a fuller conversational loop (`transport.input()`, STT, user aggregator, LLM, TTS, `transport.output()`, assistant aggregator); this project keeps orchestration and playback outside Pipecat after earlier in-pipeline TTS produced choppy speech in live Meet runs. The serial `PlaybackEngine` now streams PCM into `paplay` on `agent_output`. It mutes the output sink between utterances while leaving the microphone source unmuted.

The engine owns one retry for a synthesis timeout; the owned OpenAI speech client disables SDK retries. Streaming retries are allowed only before the first nonempty PCM chunk. A failed partial stream is stopped without replay. Cancellation closes the provider iterator and reaps the player process. Journal and fault dependencies are injected explicitly through the audio factory.

Agent replies use Pipecat's `match_endofsentence` to enforce two spoken sentences, preserving abbreviation, decimal, and closing-quote boundaries. Explicit operator `--speak` text is exempt. The full generated reply remains in graph history, including drafts that were never heard.

This composition captures through a real audio transport. CloakBrowser and Playwright operate the Meet page and browser UI; they do not supply Pipecat transport objects. Meet's remote audio arrives via the `meeting_audio.monitor` PulseAudio source, and playback reaches the browser microphone through the `agent_mic` remap of `agent_output`.

Muse `ENDPOINTING` emits model-detected speech boundaries and final transcript frames. `PUSH_TO_TALK` leaves turn delimiting to the client. `DIARIZATION` adds session-scoped speaker attribution; it does not provide verified participant identities. VAD remains a pipeline concern; interruption policy lives in the conversation loop. Current barge-in is triggered by a final transcript and stops active/queued playback; it is not yet triggered by the first detected speech frame.

Capture heartbeat and energy are separate signals. Frames with zero RMS and recent participant presence produce a journal observation, not proof of broken capture. Missing frames and speech without finals follow the existing fault/grace policy. See the [verification report](voice-reliability-refactor-2026-09-30.md) for measured coverage and live limits.

In Pipecat 1.12.0, the Muse adapter logs `speaker`, `speechStart`, and `speechEnd` events internally. It does not map speaker events to participant identities on transcription frames. A downstream pipeline cannot treat the current transcription `user_id` as verified Muse diarization. The [live smoke test](live-muse-test.md) checks final transcription in `ENDPOINTING` mode.

Pipecat 1.12.0 also includes `LangchainProcessor` in `pipecat.processors.frameworks.langchain`. It accepts a LangChain `Runnable`, extracts only the latest plain-text message from an `LLMContextFrame`, and streams `AIMessageChunk` or string output as `TextFrame` objects. It is a lightweight text-in/text-out bridge: it does not pass the full message list, tool calls, or multimodal content to the runnable. It also requires the separate `pipecat-ai[langchain]` extra, which is not part of this foundation's dependency set. For the existing LangGraph/LangChain agent, keep orchestration outside the voice pipeline until a dedicated adapter owns the message and tool contract. Pipecat's native LLM service path is the compatible option for a complete conversational pipeline.

References:

- [Pipecat 1.12.0 Muse source](https://raw.githubusercontent.com/pipecat-ai/pipecat/v1.12.0/src/pipecat/services/meta/stt.py)
- [Pipecat 1.12.0 package metadata](https://raw.githubusercontent.com/pipecat-ai/pipecat/v1.12.0/pyproject.toml)
- [Pipecat voice pipeline example](https://github.com/pipecat-ai/pipecat/blob/main/examples/voice/voice-openai.py)
- [Pipecat 1.12.0 LangchainProcessor](https://raw.githubusercontent.com/pipecat-ai/pipecat/v1.12.0/src/pipecat/processors/frameworks/langchain.py)
- [Meta Muse Voice Transcribe API](https://dev.meta.ai/docs/speech-to-text)
- [Meta streaming cookbook](https://dev.meta.ai/docs/cookbook/voice-api-fundamentals)
