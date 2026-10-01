# Voice loop reliability plan

Status: implemented 29–30 September 2026 and refactored on 30 September; see the refactor and verification report. Historical live runs: `027fe902e88d` and `8581ccc34446`. Their local logs demonstrate conversation activity, but do not measure first-audible latency or independently establish every acceptance criterion below. Next implementation: [persona and scoped memory](persona-memory.md).

## Goal

Make the admitted voice loop fast, robust, and diagnosable: replies start within seconds, transient TTS failures do not eat turns, barge-in is verified live, and a dead capture path is distinguishable from a quiet room.

Non-goals: swapping the STT→LLM→TTS cascade for a speech-to-speech provider (spike only if the cascade still misses the bar after streaming), speaker attribution, recording, and memory ingestion.

## Success Criteria

- First audible reply audio starts within seconds of a final transcript on a normal turn (no more `kok diam` gaps).
- Zero `Microphone muted by system` dialogs across a full live session.
- A single transient TTS timeout no longer loses a turn.
- Deliberate over-talk with `--barge-in` stops playback and produces a fresh reply.
- Full suite, `mypy --strict`, and `ruff` stay green; radio silence from capture becomes an attributed verdict (follow-up item) instead of an absence of evidence.

## Approach

Apply the cheapest structural fixes first, in this order: stream TTS audio straight to paplay (Vexa lesson — the engine already has the streaming branch, only the full-buffer path is wired); cap spoken agent replies deterministically to two sentences (the persona already asks for this but the model disobeyed live, so enforcement cannot depend on model cooperation); retry TTS synthesis exactly once on timeout; verify barge-in live before writing any new interruption code; then add energy-based capture liveness as a follow-up. No provider swap and no new abstraction layers in this plan.

## Steps

1. Streaming TTS → paplay.
   - Add `stream(text)` to the OpenAI speech synthesizer in `src/fakeyou/voice.py`: async iterator of PCM s16le chunks over the client's streaming speech API (verify chunk shape against the installed SDK during implementation).
   - Pass `stream_audio=create_paplay_stream_runner(...)` and enable streaming in `open_meeting_audio` (`src/fakeyou/audio.py`). Keep full-buffer as fallback when `stream` is absent (the engine already branches on its presence).
   - Treat paplay stdin `EPIPE` on cancel as expected cleanup (LMA pattern), not a fault.
   - Tests: streaming order/cancel/fallback in `tests/test_voice.py`, `tests/test_playback.py`.
2. Deterministic two-sentence cap on agent replies in `src/fakeyou/conversation.py`, before speech submission. `--speak` text is exempt (explicit operator input). Remainder is dropped for voice, never queued; full text stays in checkpoint history. Tests: 1/2/5-sentence boundaries plus the `--speak` exemption.
3. Retry TTS synthesis once on `TimeoutError` or a classified provider timeout, in `PlaybackEngine.speak` (`src/fakeyou/playback.py`), with one bounded budget per attempt. Disable additional SDK retries in the owned speech client. For streaming, retry only before any nonempty PCM was emitted; never restart an already audible utterance. Record `synth_retry` in the journal. No retry on auth/validation/playback errors. Tests: timeout-then-success, double-timeout, partial stream, cancellation, and journal wiring.
4. Rebuild the `meeting-worker` image; run the full suite plus `mypy --strict`, `ruff check`, `ruff format --check`.
5. Round 2 live verification on the test link: admit → greeting audible → two linked questions → deliberate over-talk with `--converse --barge-in` → wake-phrase run → leave. Collect transcripts, speak events, transitions, journal, checkpoints.
6. Capture activity: meter RMS on input audio frames, track startup, frame heartbeat, energy, and recent remote presence. Zero RMS with present participants produces a nonfatal `capture_silent` journal observation; everyone may be muted. It does not prove dead capture and does not degrade STT. Missing frames remain `capture_fault`; speech without finals can produce `stt_fault`. Preserve recent positive presence across unknown updates until its TTL expires.
7. Append the voice-loop findings to `docs/reference-lessons.md` as a dated section.

## Validation Plan

- Unit: `uv run pytest -q -p no:cacheprovider` (expect all green), `uv run mypy --strict src`, `uv run ruff check src tests`, `uv run ruff format --check src tests`.
- Offline: existing container probes still pass (browser render RMS high, `agent_mic` unmuted after muter cycles); new streaming path covered by unit tests with a fake paplay on `PATH` (established pattern in `tests/test_playback.py`).
- Live round 2: the script in step 5. Record remote hearing separately from `speak_started`, which is local dispatch/unmute before first PCM. Journal first-chunk latency measures the provider path only. Over-talk should yield stop + `speak_dropped` + a fresh reply; verify audible stop separately. Check UI/log evidence for `Microphone muted by system` and an ended lifecycle.
- If round 2 still shows multi-tens-of-seconds replies, the plan fails over to a time-boxed speech-to-speech spike instead of further cascade tuning.

## Risks / Open Questions

- OpenAI streaming TTS chunk sizes and first-byte latency are provider behavior. Buffered synthesis is a compatibility path when the synthesizer lacks streaming support, not an automatic recovery after a stalled partial stream. Replaying a partially emitted utterance would repeat speech.
- Two-sentence truncation can cut an answer mid-thought; accepted for voice (checkpoint keeps full text), flagged here so the reviewer can veto the cap length.
- Barge-in step writes no code until a live gap is observed; if none appears, step 5 closes it with evidence only.
- The topology remains isolated per worker. Only the playback sink is muted between utterances; muting the microphone source triggers Meet's system-mute dialog. Preserve the primary audio/conversation failure if leave cleanup also fails; leave failure during a normal duration/owner-stop path still reports `browser_failure`.

## Historical live evidence

Run `8581ccc34446` has 13 agent invocations, 3 capped replies, and local log events for 13 speech starts, 9 finishes, and 4 drops. It ends with `duration_limit`, exit 0. The inspected log contains no literal `Microphone muted by system` message. This is not proof that an unlogged dialog never appeared.

The historical journals lack `synth_*` events because production playback did not receive the journal dependency; the refactor fixes that wiring. Durations in those journals are agent/LLM timings, not remote audio latency. A new hosted acceptance run should collect the now-wired synthesis events plus remote hearing and interruption timing.

## Sources

- https://github.com/Vexa-ai/vexa
- https://github.com/aws-samples/amazon-transcribe-live-meeting-assistant
