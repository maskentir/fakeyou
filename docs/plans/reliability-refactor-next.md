# Reliability Refactor Completion and Next Milestones

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the interrupted reliability refactor (9 red tests, 1 slow suite) and land every B0–B10 finding on a green gate.

**Architecture:** Keep the noon Codex direction (degrade-and-continue conversation, all-must-pass preflight, audio-owned deaf evaluation, dormant streaming capability); repair each half-finished edit to its pinned contract; replace the 180-second unknown timer with 2-consecutive observation failures.

**Tech Stack:** Python 3.12, Pipecat 1.12.0, pytest, mypy --strict, ruff.

**Spec:** Temuan Vexa/LMA B0–B10 (user message 2026-09-29) plus the four prior plans: [meeting-runtime-plan.md](meeting-runtime.md), [audio-runtime-plan.md](audio-runtime.md), [conversation-plan.md](conversation.md), [implementation-plan.md](implementation.md).

## Global Constraints

- Committed tests are contracts; a changed expectation needs a stated reason tied to the findings.
- `uv run pytest` (default markers), `uv run mypy`, `uv run ruff check .`, `uv run ruff format --check .`, and `docker compose --profile production config --quiet` must all pass.
- Never commit, push, or tag; leave work uncommitted for review.
- No live provider calls in unit verification; live acceptance stays a separate gated milestone.

---

## Starting state (verified 2026-09-29)

- Morning: 451 passed, 14 deselected, all static gates green.
- Noon: parent + 3 review subagents edited concurrently, all died on usage limits mid-edit.
- Now: 442 passed, 9 failed, suite takes ~196s (one test burns 180 real seconds).

## Contract decisions (locked)

1. Preflight refuses unless every probe succeeds; refusal reason stays `infra_failure` (B9 explicit infra reason, B8 exit 3). Mixed probes refuse.
2. A single UI observation failure is tolerated; 2 consecutive observation failures end the session as `browser_failure`. Legit `unknown` states keep HEAD behavior (tolerated, no transition).
3. A failed conversation loop disables replies and keeps listening; transcript-callback isolation stays in `_TranscriptObserver`.
4. Deaf evaluation lives in the audio session (`maintenance_tick`); the injected `deaf_monitor` seam stays removed.
5. Preflight passes hostnames (`preflight_hosts`); the kwarg name, the CLI pins, and B9 DNS+TLS semantics agree.
6. TTS streaming stays a dormant, unit-tested capability; no CLI flag until Linux-audio end-to-end verification exists.
7. Leave problems (exception or unsuccessful status) map to `browser_failed`; the `leave_failed` event stays removed.

## Part A: reconciliation tasks

### Task 1: leave mapping back to `browser_failed`

**Files:**
- Modify: `src/fakeyou/runtime.py` (`finish`, exception path + status path)
- Modify: `src/fakeyou/lifecycle.py` (remove `leave_failed` kind, transition, apply branch)

- [x] Send `browser_failed` (not `leave_failed`) when `leave_meeting` raises and when its status is not `left`.
- [x] Run: `uv run pytest tests/test_runtime.py -q -k 'leave_failure or leave_crash'`
- [x] Expected: 2 passed (committed pins restored).

### Task 2: consecutive observation failures escalate, single blip tolerated

**Files:**
- Modify: `src/fakeyou/runtime.py` (`monitor`: replace `unknown_since`/180s block with a consecutive-failure counter on `checked.observation_failed`, threshold 2, reset on clean check)
- Test: `tests/test_runtime.py` (add `test_run_meeting_session_tolerates_single_observation_blip`: page `[IN_CALL, RuntimeError, IN_CALL, ENDED]` ends `meeting_ended`)

- [x] Implement the counter; legit `unknown` states must not increment it.
- [x] Run: `uv run pytest tests/test_runtime.py tests/test_runtime_audio.py -q`
- [x] Expected: all pass, suite back under ~30s (no 180s burn).

### Task 3: deaf tests through the canonical maintenance path

**Files:**
- Modify: `tests/test_runtime_audio.py` (remove `_ScriptedDeafMonitor`; give the `Audio` fake a scripted `maintenance_tick(is_busy, remote_participants)` making it `AudioMaintenance`; override `observe` per-test so it does not set stop)

- [x] Expiry test: tick 1 requests reconnect and returns `silent`; tick 2 raises `MeetingAudioError("deaf: persistent stt_fault")`; assert `audio_failure`, detail startswith `deaf:`, one reconnect request, tick count >= 2.
- [x] Repair test: tick 1 requests reconnect; tick 2 sets stop and returns `hearing`; assert `admitted_then_stopped`.
- [x] Run: `uv run pytest tests/test_runtime_audio.py -q -k deaf`
- [x] Expected: 2 passed.

### Task 4: restore journal conferral fallback

**Files:**
- Modify: `src/fakeyou/conversation.py` (restore `_with_journal` + `is_dataclass`/`replace`/`cast` imports; `self._responder = _with_journal(responder, journal)`)

- [x] Responder with explicit journal keeps it; dataclass responder with `None` journal receives the loop journal; anything else passes through untouched.
- [x] Run: `uv run pytest tests/test_conversation.py -q`
- [x] Expected: all pass, including `test_loop_passes_journal_through_to_responder`.

### Task 5: preflight hostnames

**Files:**
- Modify: `src/fakeyou/cli.py` (`provider_probe_targets` back to `provider_probe_hosts`, `_PREFLIGHT_TARGETS` back to `_PREFLIGHT_HOSTS`)

- [x] Run: `uv run pytest tests/test_cli.py -q -k 'preflight_hosts or conversation_receives_journal'`
- [x] Expected: 2 passed.

### Task 6: remove half-integrated streaming flag, cover dormant streaming

**Files:**
- Modify: `src/fakeyou/cli.py` (remove `--stream-tts` flag, validation, `main` pass-through, `streaming_tts` partial kwarg — `open_meeting_audio` has no such parameter)
- Test: `tests/test_playback.py` (add streamed-speak happy path with fake stream synthesizer + stream player; add stream-play-failure emits `speak_failed` and records a fault)

- [x] Non-streaming default paths unchanged (`streaming=False`).
- [x] Run: `uv run pytest tests/test_playback.py tests/test_cli.py -q`
- [x] Expected: all pass.

### Task 7: playback regressions

**Files:**
- Modify: `src/fakeyou/playback.py` (unpack `_, stderr = await proc.communicate(audio)`; `_unmute` back to play-through: emit `mute_failed`, record fault, continue playback)

- [x] Run: `uv run pytest tests/test_playback.py -q`
- [x] Expected: all pass, including mute-failure-continues and nonzero-exit tests.

### Task 8: remove dead STT progress field

**Files:**
- Modify: `src/fakeyou/deaf.py` (remove `last_stt_progress_at` from snapshot, activity state, stamping, and snapshot construction — stamped but never read)

- [x] Keep the capture-fault-before-alone ordering (finding: capture-fault suppresses left-alone).
- [x] Run: `uv run pytest tests/test_deaf.py tests/test_audio.py -q`
- [x] Expected: all pass.

### Task 9: full gates

- [x] Run: `uv run pytest -q`, `uv run mypy`, `uv run ruff check .`, `uv run ruff format --check .`, `FAKEYOU_NEO4J_PASSWORD=ci-config-check-only docker compose --profile production config --quiet`
- [x] Expected: 451+ passed with 0 failed, mypy clean on 22+ files, ruff clean, compose OK.

## Part B: next milestones (after Part A is green)

### M1: live conversation acceptance (needs hosted Meet)

- [ ] Join a user-hosted Meet with `--audio --converse` and a short duration; ask two linked questions; confirm both replies audible and the second uses first-turn context.
- [ ] Confirm transcripts, journal tape (`.runtime/journals/<meeting>.jsonl` with `synth_*`/`agent_invoke` pairs), and terminal result agree.
- [ ] Stop mid-reply; confirm no late speech, ended lifecycle, no leftover containers.

### M2: live reliability acceptance

- [ ] Preflight refusal: point the worker at an unroutable network (or block provider hosts) and confirm `infra_failure` with exit code 3 before any browser opens.
- [ ] Deaf termination: join with STT credentials revoked and confirm the session ends `audio_failure` (deaf) within grace instead of sitting silent until duration.
- [ ] Fault report: confirm the terminal output carries a `faults` summary after a real provider error.

### M3: streaming TTS end-to-end (P2)

- [ ] Reintroduce the CLI flag only with Linux-audio verification: streamed reply fully audible via `paplay`, journal shows `streaming=True` with first-chunk latency, generation drop still interrupts cleanly.

### M4: deployment and CI

- [ ] Run the real GitHub CI workflow end to end; schedule the meeting worker (duration caps, journal rotation across meetings, remote stop).
