# Page epoch review corrections — revision 3

PublicationContext OFF; Phase 5B NO-GO; zero new provider requests; US$0.
No ready/merge or production/compliance claim. The binding #205 contract,
historical audit/evaluation reports, cassettes, labels, fixtures, ledgers,
scorecards, SLOs and configuration defaults are unchanged.

Reviewed head: `c7507804`. Tested implementation: `76f047bf58c47298f914560ba760a3909f3410bb`.
Delivery head and its exact CI runs are recorded in draft PR #216.

## Reproduced finding and fix

Two controls were RED on the reviewed code: after Python `begin_epoch()`, a
new page's sampler generation 1 remained behind the old generation 8; a late
old-page batch with a larger generation could replace the new-page sample.
Both now pass. RED evidence is recorded in the JSON companion.

Native script initialization creates a new page-session identifier. The drain,
collection messages, samples, emitted frames and active sampler reports carry
that identity. A page-session identifier is provenance, not a provider grant or
proof of participant identity.

Python scopes each sampler by `(page_epoch, page_session, stream_id, generation)`.
Sequence/media monotonicity remains enforced within an incarnation. A larger
counter from a retired page is refused, not treated as freshness. A lower
counter from the valid new page is accepted without overwriting old captures.

`begin_epoch()` stops old confirmations, clears frame/sampler caches and old
clock exchanges, and preserves immutable analyzed capture records. New clock
mapping must come from the new page. Owner lookup and frame-cache identity are
page-scoped, so reused stream IDs do not relabel the old evidence.

The production drain captures the expected Python epoch before awaiting page
I/O. An in-flight old result is refused before it can add a clock exchange,
roster update or sample. A new session observed on the current drain starts a
new epoch and forces assignment refresh even when the stream assignments have
identical values. Previously retired sessions cannot start it again.

Every part must match its batch's page session. Mixed/relabelled old messages,
samples, frames and liveness records are rejected. Frame decode completion and
the remainder of an old batch are also checked after suspension.

Legacy untagged local fixtures remain available while unbound. The production
drain does not accept an untagged batch for a new epoch. No legacy fixture was
rewritten to disguise the new protocol requirements.

## Verification and limits

Local suite: **12,769 passed, 8 skipped**, no failures. Ruff check/format, mypy
(759 files), seven import contracts and deptry passed. Browser/live/container/
postgres/simulator excluded locally. Node tests run the actual perception asset
with fake APIs and two fresh VM page contexts; the new page resets generation to
1 with a different page-session ID. No browser or Meet page is opened.

The suite retains seven warnings, including the previously reported fake
Cartesia websocket cleanup warning. This correction does not claim to fix it.

All existing 119 audit references resolve to passing executed cases. The r2
semantic classification remains **98 covered / 20 partial / one mismatch**;
new page controls are evidence additions, not credits for the unapproved NC-76
proposal or for production readiness. This is a focused lifecycle revision, not
an assertion that every clause of every partial control now passes.

## Separate review workstreams

- A new docs-only draft proposes explicit NC-76 wording and I7 clarification.
  It compares local speculative preparation with strict pre-existing-audio-only
  behavior, documents latency/privacy tradeoffs, and specifies proposed negative
  controls. It changes no active contract, prompt, schema or runtime behavior.
- V6 remains an unresolved activation gate: writer-origin timestamps cannot make
  a blocking physical write bounded or safely revoke it on cancellation.
- Y2 remains unresolved. Provider mode fails closed; local preparation/attempt
  records are not a physical provider request/budget ledger.
- Product liveness is a separate evaluation plan. The model already uses
  `SpeechPlan.claims` (session claim headers and `say`), while the visual parser
  expects a different `CLAIMS:` schema. The current test helper manually appends
  that schema; the labelled 7/19 coverage does not establish actual model-flow
  availability. EN/ID corpus adequacy and prompt/adapter evaluation remain
  unmeasured where evidence is missing. No provider experiment is authorized.

## Executed page controls

| ID | Test | Passing cases |
| --- | --- | --- |
| PE-01 | `tests/test_sensors_epoch.py::test_new_page_epoch_accepts_reset_sampler_generation_and_counters` | 1 |
| PE-02 | `tests/test_sensors_epoch.py::test_retired_page_cannot_replace_new_page_with_a_higher_generation` | 1 |
| PE-03 | `tests/test_sensors_epoch.py::test_new_batch_cannot_relabel_retired_page_parts` | 4 |
| PE-04 | `tests/test_sensors_epoch.py::test_generation_regression_is_still_rejected_within_the_same_page` | 1 |
| PE-05 | `tests/test_sensors_epoch.py::test_drain_in_flight_cannot_cross_a_python_page_epoch` | 1 |
| PE-06 | `tests/test_sensors_epoch.py::test_reload_detected_by_the_drain_starts_a_new_page_and_reassigns_streams` | 1 |
| PE-07 | `tests/test_sensors_epoch.py::test_new_epoch_does_not_reuse_the_previous_page_clock_exchange` | 1 |
| PE-08 | `tests/test_sensors_epoch.py::test_missing_page_session_cannot_bind_a_new_production_epoch` | 1 |
| PE-09 | `tests/test_sensors_epoch.py::test_decode_in_flight_does_not_admit_the_rest_of_a_retired_batch` | 1 |
| PE-10 | `tests/test_samples_ledger.py::test_page_epoch_scopes_generation_and_rejects_old_page_records` | 1 |
| PE-11 | `tests/test_samples_ledger.py::test_stream_owner_lookup_does_not_relabel_old_page_evidence` | 1 |
| PE-12 | `tests/test_sensors_generation.py::test_sampler_reassignment_and_old_completion_preserve_the_successor` | 1 |

JUnit SHA-256: `450b171d8866da435571392418907d8a747e8a7d382a6a68e5ca8da0edd02191`.

The separate proposal is linked from the PR description. Its publication grants no implementation or activation approval.
