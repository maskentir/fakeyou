# Session scorecard contract v2

Approved by the owner on 2026-11-11. This changes measurement, not the latency budgets or the meeting participation policy.

## Audio timing

Measure from the participant's recorded speech stop to the first successful PCM write to the playback writer. This is a local output boundary, not proof of reception by another Meet participant. Keep TTS first-PCM timing as a separate diagnostic.

- `first_audio_p50_seconds` and `first_audio_p95_seconds` include a cue or an answer.
- `first_answer_audio_p50_seconds` and `first_answer_audio_p95_seconds` exclude cues.
- Both pairs retain the budgets of 3.5 seconds p50 and 6.5 seconds p95.
- `answer_audio_coverage` must equal 1. Missing answers cannot disappear from a latency percentile. Do not round this ratio before scoring.

A cue at 1.5 seconds and answer at 8 seconds passes the response gate and fails the answer gate. A cue followed by a failed answer fails coverage. Preparation, an empty stream, and a cancelled write produce no output timestamp. Every output timestamp needs its own transcript identity.

Only turns that require speech enter coverage. Deterministic silent, action-only, leave-only and chat plans do not require voice output. Discarded speculative drafts do not create an answer obligation. An accepted silent draft and an intentionally paused or cancelled reply are also excluded when there is no output or failure. Failure remains visible despite cancellation. Audio already emitted remains in the latency sample even if the participant interrupts it later.

`latency_by_evidence` groups samples and expected-answer counts by the deterministic plan's evidence needs. It does not infer cache hits from a fast response. Comparing cached and fresh evidence still requires scenarios with known evidence state. No cache freshness policy or evidence latency budget changes here.

## Questions

Count completed replies by transcript turn, not playback sentence clips. Keep the raw trailing-question count and rate. Score `unnecessary_question_rate` against 0.1.

An exception requires a question claim that passes the existing speech checks, cites a permitted brief question and occurs in a non-proactive turn. Its sentence must match that brief question after case and whitespace normalization. Unknown refs, a disabled verification gate, unavailable permission, objectives alone, unrelated questions and unverified paraphrases receive no exception.

Correlate authorization with the actual output using transcript identity, sentence index and a whitespace-normalized SHA-256 digest. Do not retain new reply text in the journal. Whole replies receive an aggregate digest only after every question sentence is checked. A permitted question cannot exempt an unrelated extra question in the same reply.

## Faults and historical reports

Write one `fault_recorded` event per occurrence. Keep grouped in-memory summaries with the first detail and cumulative count. V2 reports retain operation so two timeout sources remain distinguishable. Keep the 6.0 faults/hour budget.

New journal rows carry `answer_boundary: true`, the marker that the journal records the answer output boundary and the question provenance. A journal without that marker cannot be measured by the gates that need those events: they stay unscored instead of passing or failing. A new ingestion event appended to an old session must not upgrade it.

There is one report contract. The earlier contract's numbers are kept as an archive, `docs/reports/2026-11-11-contract-v1-scorecards.json`, because the journals that were measured under it stay readable but can no longer be recomputed with it. Historical faults suppressed before journaling cannot be reconstructed.

## Validation

Offline regressions cover delayed and failed writes, cue identity, independent audio gates, missing-answer coverage, split clips, whole replies, extra questions, permission failures, cancelled and speculative work, repeated faults and historical versions.

For live validation, use the same scripted turns in fresh sessions: an ordinary answer, an answer using a known existing screen description, a new screen lookup and a new participant-camera lookup. Record sample counts and both audio latencies. Check poll duration, observation freshness and dropped frames alongside timeout counts before concluding that the earlier payload/timeout change fixed live reliability. This change does not claim a new live pass.
