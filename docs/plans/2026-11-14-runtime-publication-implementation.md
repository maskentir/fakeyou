# Phase 5A — Offline runtime publication wiring

This implementation follows the design merged in #197 (`43ca1c0`). It connects
perception provenance, prepared sentences, the playback queue, and the first PCM
write. All validation uses fake providers and clocks. Paid execution remains
zero requests and US$0; the #194 experiment ledger stays closed.

## Composition and ownership

`MeetingEvidence` returns a `GatheredEvidence` mapping. Its model-facing payload
stays unchanged. Its separate, immutable provenance contains the gather attempt,
visual kind, snapshot, source timestamp, observation timestamp, and explicit
mapping gaps.

`TurnSink` combines that provenance with the final prepared sentence after
scrubbing and indexing. It creates a fresh publication ID and preserves the
meeting, turn, request, transcript, sentence index, and normalized SHA-256 hash.
This conservatively binds the whole reply's evidence to each sentence. It does
not establish that an individual claim is semantically supported.

The actor, meeting audio session, and stream fleet forward the immutable binding.
Playback checks the binding's sentence identity and carries it through queueing,
gating, and local playback events. `SpokenLog` records the same identity and hash.
The fleet routes binding to the session that prepared the audio.

Session construction accepts an optional `StreamComponents.publication` context.
A caller can inject the clock, bounded visual gate, and publication ID factory.
Without a context, existing playback behavior stays unchanged. No live provider,
participant observer, or global enforcement switch is enabled by this PR.

## Gate and measurement

The visual gate runs once per clip, after queueing and thawing, immediately before
its first PCM write. It checks cancellation, supersession, and freeze again after
awaiting revalidation. Invalid or unresolved current-state evidence prevents the
original clip. Playback emits the prevented outcome; it does not rewrite the
sentence or issue a provider request to refresh it.

Direct speech that never passes through `TurnSink` has no sentence binding. A
caller that knows its line carries no visual claim declares it with
`speak_no_visual`. Playback admits that declared clip without consulting the
visual gate. Undeclared direct speech and unbound prepared replies stay
unresolved and are prevented while a context is active. The declared callers are
the initial greeting, the recovery apology, and the attendance, participant
count, and own-action state answers. Presenter and screen-title state answers
stay undeclared because they describe the current screen; with a context active
they are prevented until they carry their own evidence binding.

Historical wording or an abstention requires separate upstream preparation and
binding. A missing participant observer never blocks otherwise safe speech.
Historical and no-visual scopes do not require a current-state liveness check.

Capture time and observation time are separate. `capture_clock_domain` describes
the original timestamp; `capture_mapping_clock_domain` describes the mapped
monotonic timestamp. Missing mapping is explicit. Frame age uses capture time,
including uncertainty, and never substitutes observation time. Revalidation
retains its actual timestamp and uncertainty. All evidence is checked again at
the final local boundary after asynchronous revalidation completes.

`measure_publication` requires an independent, consented participant observation
before reporting an observed compliant result. Local PCM telemetry alone is
insufficient. Current-state measurement also needs capture mapping, provider
attempt identity, a recent validation, and continuity proof through audible end.
Known violations produce an observed noncompliant result. Missing proof or
threshold-crossing clock uncertainty produces a not-measurable result. A prevented
clip requires an explicit no-write receipt and preserves invalid versus unknown.
A vetoed clip that a valid, consented participant observation still heard is an
observed violation: the result is observed noncompliant with
`published_despite_gate_invalid` or `published_despite_gate_unknown` and keeps the
gate reason. A veto with no observation, an insufficient observation, or a local
no-write receipt that contradicts the participant observation stays not
measurable.

## Deterministic proof map

The numbers below refer to the 19 tests specified by #197. Several requirements
share a fixture or are covered by more than one regression test.

| Design cases | Proof location |
| --- | --- |
| 1, 2, 3 — stale, fresh, queue delay | `tests/test_playback_publication.py` |
| 4, 5, 6 — cancellation, barge-in, bounded timeout | `tests/test_playback_publication.py` |
| 7 — prevented clip adds no PCM after prior audio | `tests/test_playback_publication.py`; existing fade-out tests |
| 8 — local telemetry preserves identity without claiming audibility | `tests/test_turn_graph_publication.py`; `tests/test_playback_audio_started.py` |
| 9, 10, 11 — wrong identity, uncertain clock, absent observer | `tests/test_publication_measurement.py`; `evals/test_publication_measurement_eval.py` |
| 12 — identity survives orchestration and playback | `tests/test_turn_graph_publication.py`; `tests/test_audio_publication.py` |
| 13 — production never imports evaluation adapters | `tests/test_publication_imports.py`; import contracts |
| 14 — prevented, observed noncompliant, not measurable | `tests/test_publication_measurement.py` |
| 15 — observer unavailable versus visual timeout | `tests/test_playback_publication.py`; `tests/test_publication_measurement.py` |
| 16 — provenance before scrub, final binding afterward | `tests/test_context_evidence.py`; `tests/test_turn_graph_publication.py` |
| 17 — repeated sentence, distinct publication attempts | `tests/test_playback_publication.py`; `tests/test_publication_measurement.py` |
| 18, 19 — old capture/recent observation, missing capture mapping | `tests/test_playback_publication.py`; `tests/test_publication_freshness.py`; `tests/test_publication_measurement.py` |

Review of #198 added two regressions outside the 19 design cases:

| Case | Proof location |
| --- | --- |
| Declared no-visual direct speech plays under an active gate; undeclared direct speech and unbound prepared replies stay prevented | `tests/test_playback_publication.py`; `tests/test_audio_publication.py`; `tests/test_runtime_audio.py`; `tests/test_planner.py` |
| Gate veto with a valid participant observation is observed noncompliant; without or with insufficient or conflicting observation it is not measurable | `tests/test_publication_measurement.py` |

## Remaining live integration gaps

Production evidence currently supplies neither a measured capture clock mapping
nor a provider attempt ID. These fields remain absent. Enabling the gate with
that evidence leaves current-state validity unresolved and prevents the original
clip; it cannot produce a freshness pass.

There is still no participant audio adapter, consent source, measured remote
clock mapping, or participant-side scene continuity source. Participant compliance
therefore remains not measurable in live operation. The fake observer receipts
in these tests are deterministic controls, not live benchmark results.

No dataset annotations, historical scorecards, simulator recordings, target SLOs,
or experiment ledger entries are changed. Phase 5B remains unauthorized.
