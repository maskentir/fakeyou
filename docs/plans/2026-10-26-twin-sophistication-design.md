# Twin Sophistication Design

Date: 2026-10-26. Owner decisions delegated to the agent. Evidence: live sessions
`live-yfv-1` and `live-yfv-2`, seven-area audit (2026-10-26).

## Principle

Policy lives in structure, in this order: (1) topology (which agent/channel can
even reach a tool), (2) typed gates outside the reply LLM (judges, stamps,
interceptors, budgets), (3) model discretion last. Every live failure inverted
this order. No wordlists or prompt-only prohibitions for safety-relevant
behaviour.

## Decisions

### D1 Channel policy (audit P1, P2)

- `meet_send_chat` is reachable only when the current turn is a chat turn
  (replies flow through the chat channel; the tool is stripped) or when the
  turn judge stamps the trigger event `requests_chat=true` (the speaker asked
  the twin to write in chat). All other speech/attention turns have the tool
  stripped from the model request, and a `wrap_tool_call` interceptor refuses
  it as a backstop. The attention (proactive) agent never gets the tool.
- The meeting view stops presenting pre-join chat as live: backlog lines leave
  `chat[]`; the view carries only a count plus a note that pre-join chat is
  history. `look_back_chat` retrieves it on demand. Backlog is never
  auto-answered.
- The twin can see its own actions: successful Meet actions write own lines
  into the timeline/record/history, so "already sent/answered" is visible
  state, not something the model must remember.

### D2 Tool governance (P4, P10, P11)

- One capability registry is the source of truth per tool: grants (persona
  capability id, brief permission), channel scope, per-turn cap, session
  budget, escalation-on-result, outcome mode. Assembly, middleware, prompt
  rendering, and dispatcher all derive from it. Grant contradictions are
  join-time errors, never silent no-ops.
- `observe_meeting` stops being model-callable (the view is already injected
  every call); it stays synthetic-only with hash-skip when unchanged.
- Evidence tools are once-per-turn by default. Strong-model escalation happens
  only on productive results (parsed result codes), never on refusal/empty.
- Perception tools join the grant model (`perception:vision`); they are no
  longer default-on without a grant decision. For this iteration the default
  persona grants it, so live behaviour is unchanged, but the grant is explicit.

### D3 Latency (P4)

- Instrument true TTFA: user-speech-stopped to first twin playback, plus phase
  timings (STT endpointing, judge, per-model-call, TTS first chunk). Retire
  `agent_finished` as a latency metric.
- Plain answers take one small-model call. SLO: TTFA p95 <= 4s excluding STT
  endpointing, which gets its own budget line.

### D4 Floor control (P3, P5)

- Stop fast-path: while the twin speaks, a small multilingual stop lexicon on
  >=1-word interims yields the floor immediately (stop playback + cancel
  speech + generation bump), journaled as `decided_by: stop_lexicon`. Anyone
  may stop the twin; a false stop is cheap, a missed stop is the observed harm.
- Judge rule: imperatives to the current floor holder address the floor holder.
- Repair the dead final-yield path (wake phrase must not disable it); stamp
  judge tasks with the speech generation and cancel stale ones; journal
  rejections and interim texts.
- Interrupted sentences are excluded from `last_twin_text` repeat source;
  overlap-aware final dedup; a just-replayed sentence is never replayed again.
  Budget: stop-to-silence p95 < 1s.

### D5 Participation (P6, P7)

- The judge window carries roster facts (participant count, sole-respondent).
  In a call where the twin is the only plausible respondent, a finished
  question is the twin's to answer. Room logistics checks are split from
  room questions; an unanswered everyone-question becomes answerable after a
  delay (15s, the existing moments pattern).
- The grounding verifier judges against the recent window including
  intervening human answers (the real p10 class), not the trigger line alone.
  Close the ba90fec loop: a unit case for `on_topic=False`, real verifier in
  replay, fresh report.
- Judge-down fallback must be strictly more conservative than the judge
  (`fallback_named` no longer speaks at confidence 1.0 on quoted mentions).

### D6 Actuation (P8, P9)

- Honest outcomes: tool results say queued, never delivered. Outcomes feed
  the next turn's view via the own-action ledger; failures become system
  notes. `meet_check_action(ref)` exposes ledger state to the model.
- Phase-split timing (prepare/typed/sent/confirmed, queue_ms separate),
  per-action deadline (~15s), retry only before Enter, never retype after an
  unobserved Enter; `effect.detail` preserved into the journal.
- Chat text is inserted instantly (`insert_text`), not typed at 90ms/char.
  Human-looking typing is not a requirement.
- Confirmation target state: protocol own-device event once U5 plumbing is
  fixed; DOM echo remains as fallback signal, not the only one.
- SLOs measured on real journals: p95 <= 12s, max <= 30s, success >= 95%.

### D7 Tool set (P11)

Existing, re-governed: `recall_meeting_context` (prefetch or once/turn),
`look_at_screen` (cached reading first, once/turn, includes web_page/code),
`look_at_participant` (once/turn), `meet_send_chat` (D1),
`meet_raise_hand`/`meet_lower_hand` (state-target idempotent).
New: `look_back_chat`, `meet_check_action`, `meet_react` (<=1/10s),
`meet_mute`/`meet_unmute` (state-target + readback), `meet_leave`
(confirm window, irreversible). Deferred: `meet_present` (zero code today;
needs source-approval design). Never twin tools: captions, pin, participant
management, rename, browser MCP.

New Meet executors (react/mute/leave) ship behind the registry with unit and
browser tests; they are enabled in live briefs only after selector proof in
the L-matrix.

### D8 Eval and observability (P12, P13)

- Tool-bound live replay gate: production tools with recording dispatchers;
  per-transcript assertions (no chat mirroring, <=1 look, TTFA, stop ms,
  side-effect-free declines). `live-yfv-2` journal is a known-failing fixture
  for actuation budgets.
- Journals for local/live sessions carry text, judge `heard`, interims,
  tool result codes, phase timings, and token usage. Style/meta judge plus
  cross-turn repeat detection in replay.
- Live verification runs the L1-L19 matrix from the audit.

### D9 Correction after the bbz live rounds: planned turns, settled actions

The first implementation still let one free-form reply loop mix evidence,
meeting actions, and speech. Live evidence showed the failure shape: evidence
calls could starve a stamped action, an out-of-set emoji could burn 39s, a
queued action could be narrated before it settled, and an always-available
status tool could keep a turn alive until its stream deadline. The correction
moves sequencing into the turn graph:

- The judge emits a typed turn plan, not only a reply decision: the stamped
  meeting action, the exact Meet reaction glyph, and the evidence classes the
  line actually needs. Chat turns receive the same plan before responding;
  chat admission still comes from the existing mention gate.
- Planned non-leave meeting actions execute mechanically in the turn graph
  before the reply model runs. The reply sees the settled outcome, so speech
  cannot precede the fact it describes. Planned leave runs after the turn's
  speech or chat reply has been delivered, so a farewell is not cancelled by
  the leave itself.
- Tool binding is phase-based. An action phase exposes only the stamped
  action; an evidence phase exposes only the judged evidence tools; after an
  action, no further tools are exposed in that turn. Model-called meeting
  actions remain a fallback for unplanned turns and now settle synchronously,
  except leave, which is scheduled and released by turn settlement.
- Budgets count evidence results, not every tool message, so evidence can no
  longer consume the turn's ability to perform its stamped action.

This supersedes the queued-first wording in D6 for model-visible tool
results. The dispatcher still supports queued system requests, but a reply
model is not asked to narrate an unsettled meeting action.

## Non-goals

- No new STT/TTS providers. No speculative interim drafting until TTFA
  instrumentation explains the 7-28s outliers. No VAD ducking until self-echo
  correlation is instrumented. No `meet_present` this iteration.
