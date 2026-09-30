# Conversation refactor — 28 September 2026

The refactor replaces overlapping graph calls with one serial consumer and keeps early transcripts until the meeting is ready. Native LangGraph still owns message history, Pipecat still owns audio, and the adapter only connects their contracts.

## Changed behavior

- Typed transcript events are separate from native frame objects. Identical delivered payloads share an id; repeated speech at another timestamp remains a new utterance.
- A single graph invocation writes each thread. Pending finals are processed together, preserving their order. An outdated answer is suppressed before TTS submission.
- Owner, twin, and meeting identify the conversation checkpoint. Native message ids and metadata preserve transcript provenance.
- Native assistant text extraction replaces custom content-block parsing. Native tool callbacks replace scanning the whole history for old tool calls.
- Conversation startup waits for microphone readiness. Stop cancels a blocked greeting and prevents the conversation from binding afterward.
- All runtime exit paths close the loop before its audio/client dependencies. Conversation failures have a separate terminal reason and nonzero CLI exit.
- `--converse` explicitly enables responses; existing audio-only smoke testing stays available.

## Verification

- 244 default tests passed; 12 opt-in tests deselected.
- 6 real CloakBrowser tests passed, including guest lifecycle and microphone controls on local fixtures.
- The Linux audio test image built successfully; all 64 conversation, runtime-audio, and CLI tests passed inside it. Tests and repository configuration fixtures were mounted read-only.
- The existing deterministic DeepEval tool-correctness evaluation passed; it is not a live persona-quality score.
- Ruff, formatting, strict mypy across 13 source files, wheel/sdist build, and Compose validation passed.
- A separate read-only review found no important actionable defect in the refactored code.

These checks use local fixtures and native LangGraph checkpoints. They do not prove a participant has heard a model-generated reply in live Meet. The earlier Stage 2 live audio evidence remains valid and distinct.

## Remaining limits

The generation guard suppresses stale replies; it does not roll back tools or retract queued speech. A model deadline cancels the graph and ends this session; external operations still need their own idempotency. Dedupe is local to one loop, and Muse does not expose a stable utterance id through the installed adapter. Native turn detection, interruption, delivery tracking, participant mapping, and continuous visual observation remain future work.

Graph history includes generated replies, including drafts suppressed before speech. It is reasoning history rather than a delivery ledger; later participation work must distinguish generated, queued, played, and interrupted output.

The next task is [validated persona and scoped memory access](superpowers/plans/2026-09-28-persona-memory-access.md), beginning with the open conversation acceptance gate.
