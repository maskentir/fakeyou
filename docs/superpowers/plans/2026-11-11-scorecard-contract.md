# Scorecard contract implementation plan

**Goal:** Measure response audio and answer audio separately, count questions per turn using validated provenance, and count every fault occurrence.

**Architecture:** Extend the existing playback journal and session report. Preserve version 1 scoring for historical journals; new journals explicitly declare contract version 2. Keep all existing numeric budgets; answer audio retains its own latency gates, so a cue alone cannot pass a session.

**Spec:** `docs/plans/2026-11-11-scorecard-contract.md` (owner-approved design, 2026-11-11).

## Constraints

- No new dependencies, no live-service changes, no changes to historical journals.
- PCM availability remains a diagnostic; output audio means the first successful nonempty write to the playback writer, not confirmed remote reception.
- Every output event carries transcript identity and cue/answer identity. Preparation and cancelled/empty output cannot create an output event.
- Missing answer output cannot silently improve the score. Unknown provenance never earns an exception.
- Question rates count turns, not sentence clips. Preserve the raw question rate and score unnecessary questions separately.
- Keep budgets 3.5 seconds p50, 6.5 seconds p95, question rate 0.1, fault rate 6/hour. Separate evidence strata remain diagnostic; no relaxed evidence budget.

## Task 1: Playback boundary and cue identity

Files: `audio/playback.py`, cue forwarding adapters, `conversation/thinking_cue.py`, `orchestration/spoken_log.py`, playback/cue tests.

- [x] Add failing tests for delayed synthesis, empty/failed output, cue preparation and cancelled cue, and transcript forwarding.
- [x] Add `playback_audio_started` after the first successful nonempty writer write per clip; retain existing lifecycle events. Journal cue as `thinking_cue_audio_started`. Forward optional `transcript_id` through `play_cue`.
- [x] Run playback and cue tests; review diff and types.

## Task 2: Validated question provenance

Files: `conversation/reply_planner.py`, `conversation/conversation.py`, validated speech tests.

- [x] Add failing tests showing model labels, unknown refs, missing permissions and unrelated extra questions never earn an exception.
- [x] Journal `answer_sentence_accepted` with transcript id, sentence index and `authorized_brief_question`. Only verified, permitted, non-proactive exact brief questions earn an exception. Correlate normalized digests with output; journal no new reply text.
- [x] Run speech and planner tests.

## Task 3: Versioned report and fault counting

Files: `observability/report.py`, `observability/scorecard.py`, `observability/faults.py`, journal/session-start wiring, targets resource, report/fault tests.

- [x] Add failing tests for cue at 1.5s plus answer at 8s, cue-only failure, wrong transcript, split sentences, validated versus unvalidated questions, repeated faults, and historical scoring.
- [x] Declare v2 on newly created journals. Add output audio latency stages and turn-based reply aggregation in v2 only. Preserve v1 semantics for historical sessions, including those with new background events.
- [x] Add v2 response and answer latency gates with unchanged budgets, unnecessary-question gate, and answer-output coverage gate. Missing answer timestamps leave latency unscored and fail coverage where a reply was expected.
- [x] Emit one fault journal event for every occurrence while retaining grouped summaries; preserve operation in v2 diagnostics.
- [x] Run targeted tests, the full offline suite, ruff, mypy and architecture checks available in the repo.

## Execution notes

Work in the clean checkout explicitly authorized by the owner's “Gas”. Keep changes uncommitted for review. Playback work may be delegated with exclusive file ownership; integration stays in the primary agent.

Final verification: 11,250 offline tests passed; 8 skipped for unavailable replay fixtures and optional voiceprint dependency. Ruff check/format, mypy and all seven import contracts passed. Independent review findings were fixed and re-reviewed. No live session or deployment was performed.

Ruling: retain the raw whole-reply contract used by the trust evaluator. Record authorization for both raw output and its configured capped prefix, with retained sentence approvals. Do not change reply shaping to solve an observability mismatch.
