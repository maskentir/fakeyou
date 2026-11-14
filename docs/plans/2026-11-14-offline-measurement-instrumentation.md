# Phase 5A — Offline Measurement Instrumentation

Implements the approved measurement contract in
`2026-11-14-live-e2e-measurement.md`, merged by #195 as `6bb8cb5`.
Authorization remains zero paid requests and US$0. This implementation uses fake
providers, deterministic clocks, and separate temporary ledgers. No dataset, provider
cassette, historical ledger, or production provider configuration is modified.

## Deliverables and boundaries

1. `evals/_publication_measurement.py` records an explicit immutable binding from a
   publication/sentence identity to its gather attempt, required visual kinds, full
   snapshot references, and provider attempt IDs. `PublicationProbe.publish` calls a
   revalidator immediately before the supplied emission boundary and validates the
   independently observed audible interval afterward. A local write alone is not a
   participant audible observation. Missing/ambiguous data cannot pass.
2. `evals/_measurement_budget.py` provides a new experiment-scoped, persistent offline
   ledger for all declared paid roles. Reservations consume aggregate request and USD
   capacity before fake transport work; retries are separate attempts; unknown bills
   retain their reserved liability after cancellation or restart. Existing #194 ledgers
   are incompatible inputs and cannot be appended to by this ledger.
3. An integration test joins a fake provider reservation and response to an actual
   `SnapshotRef`, a sentence binding, use-time revalidation, and fake audible publication.
   It preserves the distinction between all-attempt acquisition activity and the attempt
   explicitly bound to a published answer, including repeated text and superseded attempts.

These are offline measurement primitives, not a new session evaluator. They do not claim
that the production runtime has an end-to-end participant-side audio observation or a
wired publication revalidation hook. A missing production adapter remains an explicit
not-measurable gap. No live run or participant latency threshold is approved here.

## Measurable and compliant are separate dimensions

A measurement reports two answers, because a failed measurement and a failing product
are different findings.

| Situation | Measurable | Compliant |
| --- | --- | --- |
| The publication is valid and fresh | `true` | `true` |
| The publication is provably stale, or the scene changed while it was heard | `true` | `false` |
| No participant audio recording exists | `false` | `null` |
| The clocks cannot be synchronised | `false` | `null` |

`measurable` is `true` when the apparatus produced a joined, time-aligned observation:
the publication exists, its identity matches the binding, the clock domains agree, and
the timestamps order. `compliant` is `false` when the product broke the freshness or
continuity contract on an otherwise measurable observation, and `null` when there was no
measurement to judge. A product violation is never reported as an instrumentation gap,
and an instrumentation gap is never reported as compliance.

## Freshness is measured at the audible start

Frame age is `published_at - frame_capture_at`, where `published_at` is the audible start
of the sentence, never the moment the probe began. A validation is fresh when it
finished no more than 250 ms before that audible start. A timestamp that cannot be
ordered, or that lands after the publication it validates, is not measurable rather than
a pass, and no negative duration is clamped to zero.

The approved bounds are the ones in the merged measurement contract: a current-state
claim uses a frame no more than 5.0 seconds old at publication, a validation finishes no
more than 250 ms before publication, and a clock mapping carries an uncertainty of at
most 50 ms.

A historical answer is assessed against its named frame: the current-state age rule and
the current-scene continuity rule do not apply to it, and the integrity rules still do.

## Task 1: publication measurements

- Add failing tests in `evals/test_publication_measurement_eval.py` before implementing
  `evals/_publication_measurement.py`.
- The binding must include meeting, intended turn, transcript, request, sentence
  index/hash, gather attempt, required kinds, snapshots, and provider attempt IDs.
- Require exact explicit publication IDs, complete kinds, and unique identities; never
  select the latest or nearest event as an implicit join. Repeated text in distinct
  attempts must remain distinguishable.
- Every observation belongs to the gather attempt of the sentence, one required kind
  cannot carry two observations, and a no-visual sentence carries no visual evidence.
  The observed sentence identity is compared for every scope, including a no-visual turn.
- Use the approved 5.0 second age, 250 ms pre-publication validation and 50 ms
  uncertainty bounds, preserve clock domains/uncertainty, and verify
  subject/stream/epoch/generation and scene continuity through the audible interval.
  Changes during queueing, inference, or publication must remain visible. Historical
  frame-bounded answers do not require current-scene validity.
- Verify missing source data, local-write-only observations, and unsupported joins are
  not measurable rather than passed; deterministic stale or invalid observations are
  measured and reported as not compliant.

## Task 2: persistent accounting

- Add failing tests in `evals/test_measurement_budget_eval.py` before implementing
  `evals/_measurement_budget.py`.
- Declare experiment identity, roles, request/USD/concurrency caps, and fake-clock inputs.
  Reject undeclared roles or incompatible/reused experiment ledgers. No live transport
  integration is added by this task.
- Test all declared roles, background vision, retries/fallback linkage, authentication
  failure, unknown timeout/cancellation charges, reconstruction after restart, and
  competing reservations. Known charges and reserved unknown upper bounds stay separate.
- Preserve an observed charge even if it exceeds the reservation; flag the accounting
  breach and refuse further work. Do not discard a bill to make the cap appear respected.

## Task 3: integration and review

- Add a deterministic integration test covering fake provider → ledger attempt → snapshot
  → sentence → validation → fake audible publication, plus a superseded attempt.
- Validate the measurement report is JSON serializable and retains all identity joins.
- Run focused tests, the offline suite, Ruff, mypy, import contracts, and dependency checks.
- Verify protected fixtures and archives are byte-identical to merged `main`.
- Review the implementation against the approved measurement contract and open a separate
  draft PR. Do not merge implementation without review, join a meeting, or call a provider.
