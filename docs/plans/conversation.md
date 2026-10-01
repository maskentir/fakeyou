# Meeting conversation loop

Status: refactored and verified offline on 28 September 2026. Live conversation acceptance remains open. Specification: [project status](../reference/project-status.md), Stage 3.

## Goal

Connect final transcripts to the LangGraph meeting agent and queue answers in the same meeting. Preserve every accepted utterance and serialize access to its checkpoint. This is the conversation foundation; a meeting participant still needs persona, turn-taking, interruption, visual context, and tools on its actual Meet page.

## Adapter decision

Checked installed Pipecat 1.12.0 `processors/frameworks/langchain.py` against our contract. `LangchainProcessor` passes only `{transcript_key: latest_text}` with `session_id`, accepts string/`AIMessageChunk` stream tokens, and needs the `pipecat-ai[langchain]` extra. A raw graph's update dictionaries yield empty tokens through that adapter. It does not pass our message history or `thread_id` contract.

Decision: thin adapter outside the voice pipeline, in `src/fakeyou/conversation.py`. No new Pipecat extra.

## Responsibilities

| Component | Responsibility |
| --- | --- |
| `TranscriptEvent` / `transcript_event` | Typed event and thin Pipecat boundary; source metadata and payload identity |
| `LangGraphResponder` | Native messages, checkpoint configuration, tool callbacks, and assistant text extraction |
| `ConversationLoop` | One task processes pending utterances in order; stale reply suppression and cancellation on close |
| `MeetingConversation` protocol | Runtime depends on readiness, failure, and cleanup contracts |
| CLI `open_conversation` | Own model, SQLite, optional Graphiti, and loop lifetimes |
| Meeting worker | Admission, microphone readiness, stop, leave, and terminal reason |

## Contracts and tradeoffs

- Only nonempty finalized events are accepted. Before binding, events are buffered without starting the agent. Runtime binds after admission, microphone readiness, and optional greeting submission.
- Exactly one graph invocation uses a meeting thread at a time. Finals arriving during that invocation form the next batch; none are discarded by keeping only the newest text.
- A newer final invalidates the current reply before speech submission. The running graph may finish so tool results and checkpoints stay consistent. It is cancelled on session close or a 60-second response deadline, rather than restarted concurrently.
- A generation guard cannot undo a completed tool call or an already submitted utterance. No automatic graph retry occurs after a timeout. Durable action idempotency remains separate work. Barge-in stops the active playback through the engine when enabled.
- The graph stores generated assistant replies, including suppressed drafts. Playback/delivery events are not yet written back into that history; do not treat it as a record of everything other participants heard.
- Thread identity derives from `owner_id`, `twin_id`, and `meeting_id`. `HumanMessage.id` uses the event id; LangGraph owns history persistence and message replacement.
- Duplicate event ids are ignored for the lifetime of this loop. The frame adapter hashes source, original timestamp, and text. When a timestamp is absent it uses the local frame id. Muse's current adapter creates receipt timestamps, so a reconnect that retranscribes the same audio can produce a new id. This does not guarantee deduplication across reconnects or process restarts.
- `participant` stays `unknown`: the current Muse `user_id` is not verified Meet identity. Event timestamps are receipt times, not measured microphone capture times.
- Only the final `AIMessage` without pending tool calls can become speech. Native `AIMessage.text` excludes reasoning/non-text blocks. User messages and raw tool results are never spoken directly.
- `acting` is emitted by a native tool-start callback for the current generation. `queued` means TTS submission; it does not claim playback began or the remote participant heard it. `listening` describes the loop's readiness, not speaker silence.
- Runtime closes conversation tasks before audio and agent dependencies on stop, denial, removal, browser failure, cancellation, and early startup failure. Model/loop failures produce `conversation_failure`; audio errors remain `audio_failure`.
- Pending events and dedupe history are in process memory for one session. There is one response task and a per-response deadline, but no durable inbox or multi-worker admission control.

## Usage

```sh
docker compose --profile audio build meeting-worker
docker compose --profile audio run --rm --no-deps meeting-worker \
  fakeyou join https://meet.google.com/xxx-yyyy-zzz \
  --audio --converse --twin-name "Fakeyou AI"
```

`--audio` alone retains transcription and optional `--speak` testing. `--converse` enables replies to final transcripts. `--memory` requires `--converse` and `--persona`; recall and transcript ingestion are scoped to the persona's owner/twin namespace. See [persona and memory](persona-memory.md) and [memory ingestion](memory-ingestion.md).

No chat/raise-hand/vision tool is attached to this meeting agent yet. MCP's separate browser command does not become a tool on the worker's Meet page automatically.

## Tasks and verification

- [x] Typed boundary and duplicate-delivery tests.
- [x] Serialized responder, buffered startup, timeout/cancel, and assistant-only speech tests.
- [x] Native LangGraph checkpoint history and identity isolation tests.
- [x] Runtime ownership, greeting stop race, and distinct failure tests.
- [x] CLI opt-in composition, lint, typing, build, browser regression, and independent review.
- [ ] One controlled live conversation: two linked questions, audible replies, stop during reasoning, and a separately observed remote-hearing result.

Latest evidence: [conversation refactor report](../reports/2026-09-28-conversation-refactor.md). Next scoped task: [persona and memory access](persona-memory-access.md).

## Documentation read

- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence).
- [LangChain short-term memory](https://docs.langchain.com/oss/python/langchain/short-term-memory).
- [Python task cancellation](https://docs.python.org/3.12/library/asyncio-task.html#task-cancellation).
- Installed Pipecat 1.12.0 `LangchainProcessor`, `MetaSTTService`, `PipelineWorker`, and frame sources.
- Installed LangChain core `AIMessage.text`, `HumanMessage`, and `AsyncCallbackHandler`; native `add_messages` reducer.
