# Meeting audio runtime

Status: done and verified live (`audio-bua-1`). Specification: [meeting runtime plan](meeting-runtime.md), Stage 2.

## Goal

Run one Google Meet guest per Linux container, transcribe participant audio through Muse, and play an explicitly requested TTS sentence through the guest microphone. Host admission remains required. Conversation policy belongs to Stage 3.

## Contracts

- Reuse the official CloakBrowser image, native PulseAudio devices, and Pipecat `LocalAudioTransport`, `PipelineWorker`, and `WorkerRunner`.
- Browser output goes to `meeting_audio`; Muse captures `meeting_audio.monitor` at 16 kHz mono PCM16.
- OpenAI synthesis stays at its native 24 kHz rate. A serial `PlaybackEngine` streams PCM directly into `paplay` on `agent_output`; `agent_mic` remaps that monitor for Chromium. The engine mutes only the output sink between utterances, leaving the microphone source unmuted so Meet does not show an OS-mute dialog. Synthesizers without streaming support use buffered playback.
- Source of truth for sink/source names and sample rates is `src/fakeyou/audio.py`; `compose.yaml`, `docker/audio/Dockerfile`, `docker/audio/default.pa`, and `docker/audio/daemon.conf` keep literal copies in sync, and the entrypoint asserts the expected sinks, rates, and defaults at startup.
- No loopback connects agent output to Muse input. Each meeting owns its PulseAudio server and browser profile lock.
- The worker starts audio after admission, enables the microphone, then queues an optional explicit sentence. Audio failures terminate the session with their own reason and release resources.
- Browser PulseAudio environment overrides apply to the child process only. No physical host audio devices are mounted.
- API keys enter at runtime through the existing `.env`; build context excludes credentials and local recordings.
- Synthesis carries a per-request timeout (the SDK default would override the HTTP client) and the engine additionally bounds each synthesis call, so one dead request fails the utterance instead of wedging later speech. Stopping playback cancels an in-flight synthesis the same way it kills active audio.
- Synthesis uses native OpenAI HTTP streaming and casual-Indonesian delivery instructions. A timeout retries once before any PCM is emitted; a partial stream is never replayed. See [voice reliability](voice-reliability.md) and its refactor report.
- The worker bypasses the Docker Desktop embedded DNS forwarder with public resolvers; it stopped resolving mid-call during live runs.
- Keep code readable without comments or docstrings. Use thin adapters around native APIs.

## Tasks and verification

1. Add a pinned Linux image, PulseAudio configuration, ALSA default adapter, and an opt-in Compose worker. Verify startup, sink/source mapping, and process shutdown on ARM64.
2. Implement the Pipecat audio context with final transcript callbacks, explicit speech, startup deadlines, and provider/background failure propagation. Exercise native pipelines in offline tests.
3. Integrate the context with admission, stop, cancellation, microphone state, CLI flags, and lifecycle checkpoints. Verify default guest behavior remains unchanged.
4. Exercise real container audio: browser output to capture, TTS output to browser microphone, and isolation between both paths. Run Muse against a known fixture with the native transport.
5. Attempt one controlled live Meet verification when a host is available. Record local evidence separately from actual remote participant hearing confirmation.
6. Run lint, typing, offline tests, container probes, and review. Update the runbook and project status with remaining limitations.

## Documentation consulted

- [CloakBrowser Dockerfile](https://github.com/CloakHQ/cloakbrowser/blob/main/Dockerfile) and [entrypoint](https://github.com/CloakHQ/cloakbrowser/blob/main/bin/docker-entrypoint.sh).
- [PulseAudio client configuration](https://manpages.debian.org/trixie/pulseaudio/pulse-client.conf.5.en.html) and [daemon configuration](https://manpages.debian.org/trixie/pulseaudio/pulse-daemon.conf.5.en.html).
- [Pipecat OpenAI TTS](https://docs.pipecat.ai/api-reference/server/services/tts/openai), plus installed Pipecat 1.12.0 transport, worker, runner, STT, and TTS sources.
- LMA's local entrypoint and audio idempotency tests for routing lessons; no application code copied.
