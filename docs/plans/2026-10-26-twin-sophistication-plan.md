# Twin Sophistication Implementation Plan

> **For agentic workers:** implement task-by-task with TDD. Steps use checkbox (`- [ ]`) syntax for tracking. No commits unless the user asks.

**Goal:** Make the twin structurally incapable of the live-yfv-2 failure modes and give it a complete, governed tool set.

**Architecture:** Channel/tool policy moves from prose into stamps, middleware interceptors, a capability registry, and per-turn budgets. The view gains settled-state (backlog tags, own actions). Floor control gains a lexicon fast-path. Every slice lands with a failing test first and keeps the full suite green.

**Tech Stack:** Python 3.12, LangChain/LangGraph middleware, pydantic, pytest, Playwright Meet automation.

**Spec:** [2026-10-26-twin-sophistication-design.md](/Users/pushmove/Desktop/side_projects/jemboed/fakeyou/docs/plans/2026-10-26-twin-sophistication-design.md)

## Global Constraints

- Structural gates over prompt text; no wordlist behaviour except the stop lexicon (a floor-safety device, journaled).
- English only in Python files (code-rules test). No docstrings/# type: ignore additions.
- Every slice: failing test first, then implementation, then `ruff check`, `ruff format --check`, `mypy`, `lint-imports`, targeted tests, then full suite before the next slice closes.
- Existing tests that pin the old contract are updated deliberately and named in the slice summary.

---

### Slice 1: Channel gate for meet_send_chat

**Files:**
- Modify: `src/fakeyou/conversation/turn_judge.py` (add `requests_chat` to `TurnJudgement` + rule line)
- Modify: `src/fakeyou/foundation/transcript.py` (add `requests_chat: bool = False` to `TranscriptEvent`)
- Modify: wherever judgements are applied to events before the responder (`turn_ingress.py` / participation path — stamp via `model_copy`)
- Modify: `src/fakeyou/conversation/meeting_messages.py` (`request_messages` stamps `requests_chat` into the meeting event)
- Modify: `src/fakeyou/conversation/middleware.py` (replace `ChatTurnToolsMiddleware` semantics: strip `meet_send_chat` on chat turns AND on speech/attention turns without the stamp; add `wrap_tool_call` refusal backstop with code `refused`)
- Modify: `tests/test_middleware_chat_tools.py` (old pin: kept-on-speech), judge fixtures if schema-checked
- Test: `tests/test_meet_action_once.py` (extend), `tests/test_middleware_chat_tools.py`

**Interfaces:**
- Produces: `TranscriptEvent.requests_chat`, meeting-event key `requests_chat`, middleware refusal result `{"code":"refused", ...}` for out-of-channel sends.

- [ ] Step 1: Failing test — scripted model calls `meet_send_chat` on a plain speech turn; dispatcher receives nothing and the model gets a `refused` tool result.
- [ ] Step 2: Run, confirm failure for the right reason (dispatcher got the send).
- [ ] Step 3: Implement judge field + stamping + middleware gate (sync + async variants).
- [ ] Step 4: Failing-then-passing test — same script with `requests_chat` stamped event sends exactly once.
- [ ] Step 5: Update old-contract tests; targeted suites + lint/type gates green.

### Slice 2: View settled-state (backlog + own actions)

**Files:**
- Modify: `src/fakeyou/perception/view.py` (exclude backlog from `chat[]`; add `chat_before_you_joined` count; VIEW_PREFIX note)
- Modify: `src/fakeyou/orchestration/meet_actions.py` + timeline wiring (own observed sends become own `ChatLine`s / record chat entries)
- Modify: `src/fakeyou/conversation/meeting_messages.py` (own chat entries enter history as twin chat lines)
- Test: `tests/test_perception_archive.py` or view tests, meet action dispatch tests

**Interfaces:**
- Produces: view field `chat_before_you_joined: int`; own chat lines with `from_you=True` in view/history.

- [ ] Step 1: Failing tests — backlog line absent from `view.chat`, count present; own send appears in view after observed_done.
- [ ] Step 2: Implement view + plumbing.
- [ ] Step 3: Targeted suites green.

### Slice 3: Evidence governance + TTFA instrumentation

**Files:**
- Modify: `src/fakeyou/orchestration/assembly.py` / perception tool construction (`observe_meeting` no longer model-callable)
- Modify: `src/fakeyou/conversation/middleware.py` (general once-per-turn cap for evidence tools incl. `look_at_screen`; escalation only on productive result codes)
- Modify: `src/fakeyou/perception/view.py` + context injection (hash-skip unchanged view; include web_page/code text in screen view per D2 — verify against owner-screen title-only rule for non-meeting content first)
- Modify: turn graph/journal (TTFA + phase timings; rename latency fields honestly)
- Test: `tests/test_evidence_routing.py`, middleware tests, journal tests

- [ ] Step 1: Failing tests — second `look_at_screen` in one turn blocked; refusal result does not escalate route; TTFA event emitted with phase fields.
- [ ] Step 2: Implement caps + productive escalation + instrumentation.
- [ ] Step 3: Targeted suites green.

### Slice 4: Stop fast-path + repeat dedup

**Files:**
- Create: `src/fakeyou/conversation/stop_lexicon.json` (multilingual stop/pause phrases, data not code)
- Modify: `src/fakeyou/conversation/turn_ingress.py` (lexicon check on interims while twin speaking; generation-stamped judge tasks; remove wake-phrase dead return; journal rejections)
- Modify: `src/fakeyou/conversation/turn_judge.py` (floor-holder imperative rule)
- Modify: `src/fakeyou/conversation/meeting_messages.py` / `reply_planner.py` (`last_twin_text` skips interrupted; already-replayed guard)
- Test: `tests/test_turn_graph_recovery.py`, ingress/barge tests

- [ ] Step 1: Failing tests — one-word interim "Stop." while speaking yields floor (playback stopped + speech cancelled); interrupted sentence is completed once, never replayed twice.
- [ ] Step 2: Implement lexicon path + repairs + dedup.
- [ ] Step 3: Targeted suites green.

### Slice 5: Honest actuation

**Files:**
- Modify: `src/fakeyou/meet/meet_tools.py` (queued copy: no delivery promise)
- Modify: `src/fakeyou/meet/meet_action_ledger.py` (phase timings, queue_ms, deadline, retry only pre-Enter, preserve `effect.detail`)
- Modify: `src/fakeyou/orchestration/meet_actions.py` / `src/fakeyou/meet/meet.py` (`insert_text`, cheaper admission)
- Test: `tests/test_meet_actions_dispatch.py`, `tests/test_meet_actions_ledger.py`, `tests/test_report_meet_actions.py` (budgets against live-yfv-2 journal fixture must report the known failure honestly)

- [ ] Step 1: Failing tests — outcome fields present; post-Enter unobserved never retypes; queued copy carries no delivery claim.
- [ ] Step 2: Implement.
- [ ] Step 3: Targeted suites green.

### Slice 6: Capability registry + new tools

**Files:**
- Create: `src/fakeyou/conversation/capabilities.py` (registry: grants, channel, caps, escalation, outcome mode)
- Modify: `src/fakeyou/orchestration/assembly.py`, `join_context.py`, `memory/persona.py` (derive tools/grants from registry; join-time errors on contradictions; perception grant explicit)
- Create/modify tools: `look_back_chat`, `meet_check_action`, `meet_react`, `meet_mute`/`meet_unmute`, `meet_leave` + Meet executors + ledger kinds
- Test: registry tests, grant-chain tests (raise-hand refusal becomes join-time diagnosable), browser tests for new selectors

- [ ] Step 1: Failing tests — registry-derived tool list per grant combo; contradiction raises at join.
- [ ] Step 2: Registry + migrate existing tools. Suites green.
- [ ] Step 3: New tools one at a time, each failing-test-first; executors behind registry flags.

### Slice 7: Participation + verifier window

**Files:**
- Modify: `src/fakeyou/conversation/turn_judge.py` (roster in window; logistics split), `participation_graph.py` (conservative fallback), `moments.py` (delayed everyone escalation), `grounding.py` + `conversation.py` (windowed verifier payload)
- Modify: judge/proactive eval fixtures (relabelling is intended), `evals/test_proactive_replay_eval.py` (real verifier)
- Test: participation tests, moments/grounding tests, eval reports regenerated

- [ ] Step 1: Failing tests — sole-respondent unnamed question gets a reply path; human-answered trigger fails the verifier.
- [ ] Step 2: Implement + relabel fixtures + rerun live evals; publish new reports under `docs/reports/`.

### Slice 8: Tool-bound replay gate + journal completeness

**Files:**
- Modify: `evals/_conversation_replay.py` (production tools + recording dispatchers; per-transcript assertions), `evals/_budgets.py` (TTFA/stop budgets; live-yfv-2 as known-failing fixture)
- Modify: journal emitters (text, `heard`, interims, result codes, phases, tokens) behind the existing journal config
- Test: eval suite offline tiers green; live tier run manually and reported

- [ ] Step 1: Failing replay cases reproducing chat mirroring + tool loops with tools bound.
- [ ] Step 2: Implement gate + journal fields.

### Final: Verification + live matrix

- [ ] Full gates: `ruff check .`, `ruff format --check .`, `mypy`, `lint-imports`, `deptry .`, full `pytest`, `uv build`.
- [ ] Rebuild containers, join Meet, run L1-L19, write findings to `docs/plans/2026-10-26-twin-live-findings.md`.
