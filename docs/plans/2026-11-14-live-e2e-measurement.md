# Phase 5 — Live E2E Measurement Plan

Status: **draft for review, before implementation or paid execution**.
Baseline code: `021668346dcaebecac3d1dcfff919f4c45b2b31e` (merged PR #194).
This document specifies measurements and proposed acceptance thresholds. It does not
claim that the current runtime implements every required observation or passes a gate.

## 1. Decision and boundaries

Measure whether Fakeyou publishes correct, appropriately timed answers in real meetings,
with auditable acquisition activity and cost. Evaluate the final audible answer, not
only the structured inspection that preceded it.

The current authorization is **zero new paid provider requests and US$0 new spend**.
The #194 experiment remains closed: 14 outbound attempts, 11 inference attempts, and
US$0.01461 estimated from recorded usage. Its twelve-request allowance is not reusable.
Do not modify its ledger, aliases, strict rubric, provider cassette, or simulator cassettes.

Plan approval permits subsequent offline evaluation-contract and instrumentation work.
Paid execution requires a separate explicit approval containing new request and cost
limits. Credentials being available, passing CI, or merging this plan is not that approval.
This milestone performs no meeting join, recording, deployment, or provider call.

Two approaches were considered. Scoring the inspection verdict alone is inexpensive but
misses reply-layer inventions, stale publication, playback delay, and repeated acquisition.
The chosen approach joins per-claim review of the published answer to frame, acquisition,
playback, and provider records. Existing journals are reused; missing joins become named
instrumentation requirements, not guessed measurements or a second reporting evaluator.

## 2. Hypotheses and populations

1. Final factual claims are supported by independently reviewable sources, including
   the visual state at the time a current-state claim is published.
2. The system abstains when the requested detail is absent or ambiguous and still answers
   answerable questions. Safe failure responses do not masquerade as visual observations.
3. A change during inference or playback queuing cannot silently turn historical evidence
   into an assertion about the current meeting.
4. Every paid invocation, including background work and retries, is attributable to the
   meeting and the new approved budget. Unknown billing remains explicit.

Keep four populations separate: published-answer claims, inspection observations,
intended turns (including failed or cancelled turns), and all provider/acquisition attempts.
The model's verdict and `verification="unverified"` are telemetry, never truth labels.

## 3. Independent claim adjudication

Before execution, freeze scenario questions, source materials, expected answerability,
participant/stream identities, and deliberate change schedules. Controlled meeting props
and independently authored slides establish truth; model outputs cannot generate labels.
Use consenting participants and non-sensitive materials. A participant-side recording
captures what was actually audible and the relevant scene timeline.

After execution, extract every atomic factual claim from audible speech. Preserve the
original words, sentence hash/index, and audio interval. Split compound assertions so an
extra unsupported position, identity, date, or ownership claim cannot hide inside a true
sentence. Record source category (visual, meeting history, explicitly provided owner
knowledge, or runtime availability), relevance, subject identity, and temporal scope
(current or frame-bounded).
Cancelled text that was never audible is diagnostic output, not a published claim.

Two independent human reviewers assess claims against source recordings and materials.
They do not see the model's verdict, model identity, or automatic score while labelling.
The evaluated model and the implementation author cannot be the sole adjudicator.
Disagreements receive a third independent review; unresolved items stay unjudgeable.
Label values are:

| Label | Independent evidence |
| --- | --- |
| Supported | The entire atomic claim is supported, for the correct subject and time |
| Contradicted | The source establishes an incompatible fact |
| Not observable | The allowed source cannot establish the asserted detail |
| Unjudgeable | Missing, ambiguous, or unsynchronized source evidence prevents adjudication |

Not observable means unsupported by the permitted evidence; it does not establish that
the claim is false in the outside world. Relevance is scored separately: a visible project
title can be factually supported but fail to answer a question about its date.

Review final answers and `observed_facts` as separate claim inventories. A faithful
inspection does not excuse an invented final answer; an unspoken inspection observation
does not increase the final-answer claim denominator. Keep reviewer decisions, source
references, disagreement resolutions, and reasons with every claim.

## 4. Scenario matrix and sampling

The following is a measurement population, **not a provider-request allocation**.
First run an optional diagnostic pilot of one turn per scenario, only if separately
budget-approved. Inspect completeness and costs, then pause. Pilot results are reported
separately and cannot replace the frozen confirmatory population.

The proposed confirmatory population is five independent real meeting sessions with
twenty intended turns each: each scenario below once per meeting, plus three additional
S01 and three additional S03 turns. This gives 100 intended turns, including twenty
no-visual and twenty intended cache-reuse turns. Use varied scenes, subjects, props,
questions, and scenario order across meetings; do not train or tune on confirmatory data.
Keep S02 and its S03 repetitions in a valid prerequisite order. Cache eligibility follows
the real question/frame/epoch/TTL contract; a stable-looking scene alone does not guarantee
a reusable snapshot. Record legitimate ineligible misses without forcing a hit.

| ID | Scenario | Evidence to preserve / expected behavior |
| --- | --- | --- |
| S01 | Ordinary question needing no visual | Correct non-visual answer; no question-triggered vision call |
| S02 | General question about a visible person or object | Correct subject and independently visible details |
| S03 | Repeat the same visual question on a stable view | Reuse when eligible; final claims still valid at use |
| S04 | Follow-up about a different detail or subject | No answer reuse from the previous question or person |
| S05 | Small object, then an illegible or occluded detail | Answer visible detail; abstain rather than invent hidden detail |
| S06 | Multiple participants; requested camera unavailable | Resolve identity correctly; do not substitute another person's image |
| S07 | Screen-share question while the speaker changes | Answer from the correct shared screen, independent of speaker identity |
| S08 | Relevant scene change during inference | Detect invalidated evidence or use an explicit historical scope |
| S09 | Change after inference, while speech is queued | Validate at publication; no stale current-state assertion |
| S10 | Explicit question about a previously captured frame | A time-bounded historical answer can be correct after the scene changes |
| S11 | Camera turns off before publication | No current visual claim based on the previously live camera |
| S12 | Stream replacement, reconnect, or reused participant ID | Epoch/generation change invalidates the old subject/frame association |
| S13 | Injected timeout, invalid response, or provider failure | Distinct measured failure; safe response; bounded work and recovery |
| S14 | Held-out natural meeting question and follow-up | Independent relevance and claim review beyond the controlled props |

Perform all S13 fault injection offline first. Live fault injection, if approved, happens
at the local boundary; do not intentionally spend requests on authentication failures.
Use all five S13 turns to cover at least timeout, invalid output, and provider failure.
S08/S09 include both camera and screen changes across sessions. Preserve the change
time and its position relative to inference, queueing, and audible publication.

The intended path and observed canonical path are separate fields. A failed cache case
is retained as a miss or failure; it is not reassigned to make the cache cohort look good.
If any gated latency cohort has fewer than twenty measured successful answers, its gate
is inconclusive and the missing outcomes remain visible. More samples require budget
approval; do not silently extend or truncate a population to achieve a pass.

## 5. Freshness and publication boundaries

Record capture, inference start/end, use-time validation, queue entry, first answer audio,
and audible sentence start/end separately. `playback_started` is not proof of audible
output. `thinking_cue_audio_started` is not the beginning of the answer.

Define `published_at` per claim as the audible start of its containing sentence and retain
the interval through the end of that claim. Compare the relevant scene at both boundaries.
A change during publication is a separately reported mid-publication violation for a
current-state assertion. Participant-side audio is the primary E2E publication observation;
local `playback_audio_started` is a distinct internal measurement.

Proposed live-at-use thresholds, requiring approval with this plan:

- A current-state claim uses a frame no more than **5.0 seconds old at publication**.
- A subject/stream/liveness validation finishes no more than **250 ms before publication**.
- The subject, stream, epoch, generation, and answer-relevant scene remain valid through
  the claim interval. Age and matching identity alone do not prove semantic continuity.
- If current validity cannot be established, refresh evidence, explicitly bound the claim
  to the captured frame, or abstain. A historical answer is assessed against its named
  frame and does not fail merely because someone subsequently moved.

Measure rather than assume these properties. A validation at inference completion is
insufficient for S09. Conversation-floor checks do not establish visual liveness.
Frame age at publication is `published_at - frame_capture_at`, not evidence `age_ms`.
The latter currently describes acquisition/cache age and can be zero for fresh inference
even after substantial computation and queuing.

Use a common monotonic timeline for local durations; preserve UTC timestamps for audit.
Record the origin of capture time. A local frame acquisition/ingress timestamp is not
the remote camera's exposure time. If source exposure time is unavailable, label age as
local-acquisition age; do not claim sensor-to-listener latency.
Cross-device publication timing needs a measured clock mapping with uncertainty at most
50 ms. Report intervals with that uncertainty; a threshold-crossing or unsynchronized
measurement is unjudgeable, not a pass. No negative duration is clamped to zero.

## 6. Telemetry inventory and offline instrumentation contract

This table names existing evidence and proposed additions; it does not introduce code.

| Measurement | Existing starting point | Required offline work before paid execution |
| --- | --- | --- |
| Planned needs and gather attempts | `plan_selected`, `perception_evidence_started`, `perception_evidence_resolved` | Prove joins across retry/replanning and cancellation |
| Capture provenance | `SnapshotRef`, `snapshot_id`, `captured_at`, `observed_at` | Retain full identity, capture origin, and monotonic timing; do not treat `observed_at` as inference end |
| Inference duration | `vision_latency_ms` | Start/end timestamps, provider attempt identity, timeout and cancellation boundaries |
| Final evidence actually used | Gather attempt ID exists | Explicit sentence-to-evidence-attempt/snapshot binding; never infer use from the last event in the journal |
| Use-time validity | Before/after-inference snapshot checks; conversation floor checks | Observe validity and its reason at publication, independently of floor ownership |
| Published answer | Sentence index/hash and `playback_audio_started` | Align to participant-side audio; retain partial/interrupted speech and claim intervals |
| Paid activity and billing | #194 visual recorder demonstrates reserve/settle accounting | New isolated experiment ledger across all paid roles, background work, retries, and fallbacks |
| Host load | No Phase 5 series established | Timestamp CPU and memory samples at one-second intervals with host/container configuration |

Every result joins meeting ID, intended turn ID, transcript ID, request ID, sentence
index/hash, evidence-gather attempt ID, snapshot identity, and provider-attempt ID where
applicable. Evidence attempt IDs and provider attempt IDs are different namespaces.
Missing IDs, duplicate events within one attempt, missing publication observations, or
unknown source timestamps prevent the corresponding measurement from passing.
A confirmed no-answer outcome at the response deadline is a valid censored observation;
missing timing for an answer that was actually audible is an instrumentation failure.

Reuse canonical evidence classification. Save a separate measurement/adjudication artifact,
not a legacy adapter or parallel session evaluator. Existing provenance/cache aggregates
count **all resolved activity across attempts**, while path classification uses the latest
attempt. Neither aggregate proves which evidence a particular published sentence used.

Offline acceptance includes deterministic clocks and scripted changes during inference
and queueing; interrupted publication; multiple attempts; missing/duplicate timestamps;
clock uncertainty; failure outputs; and reconstruction of request counts and cost bounds.
These checks use fake providers and transport boundaries only. They do not re-record
provider cassettes or mutate the closed experiment ledger.

## 7. Metrics and proposed quality gates

Report per meeting, per scenario, and pooled counts. Zero denominators produce null.
Each rate carries numerator, denominator, exclusions, and unresolved count. Failed,
cancelled, timed-out, and missing-output turns stay in intended-turn outcomes.

| Metric | Definition | Proposed confirmatory gate |
| --- | --- | --- |
| Final-answer groundedness | Supported published atomic claims / all published factual claims | At least 95%; unresolved claims cannot pass |
| False-claim rate (evidence-unsupported) | (Contradicted + not observable) claims / all claims | At most 5%; report unjudgeable-inclusive upper bound too |
| Turns with unsupported claims | Turns with at least one unsupported claim / turns publishing claims | Report alongside the claim-level rate |
| Critical correctness | Wrong subject, invented identity, or stale current-state assertion | Zero observed violations |
| Answer success | Relevant, independently correct completed answers / intended answerable turns | At least 95%, retaining operational failures in the denominator |
| Semantic abstention recall | Appropriate abstentions / unanswerable turns with a valid visual inspection opportunity | At least 95% |
| Semantic abstention precision | Appropriate abstentions / semantic abstentions | At least 95% |
| False abstention | Semantic abstentions on answerable, operationally evaluable turns / those turns | At most 5% |
| Failure-safe behavior | Injected failures with no fabricated visual claim and a correctly bounded outcome / injected failures | 100% |
| Live-at-use compliance | Current-state claims satisfying all age, validation, and scene checks / current-state claims | 100%; zero unjudgeable publication measurements |
| Internal answer latency | Runtime `speech_stopped` to local `playback_audio_started`; empirical p50/p95 | Existing global and no-visual/cached thresholds: p50 <= 3.5 s, p95 <= 6.5 s |
| Participant-observed E2E latency | End of audible question to first audible answer in the same participant recording | Proposed additional gate: p50 <= 3.5 s, p95 <= 6.5 s |
| Completion and recovery | Session completed; pending work settles after failure/stop | All sessions complete; no orphan paid work or unbounded retry |
| Accounting completeness | Outbound attempts with an attributable budget reservation and terminal or unknown settlement / all observed attempts | 100%; approved request/cost caps never exceeded |

Groundedness includes unjudgeable claims in its denominator. Report unsupported-claim
lower and upper bounds, with unjudgeable claims included in the upper bound. No
unresolved adjudication or missing source recording can be omitted to pass a gate.
Any unjudgeable published claim blocks a passing correctness decision; bounds remain
available for diagnostic reports. A null rate is not a passing rate.
Semantic abstention and operational unavailability are separate populations; report their
counts and combined response coverage so provider failures cannot improve abstention scores.

For latency, retain internal first-audio and first-answer-audio metrics separately from
participant-observed E2E latency. The latter uses the question end and answer onset in
the same participant-side audio clock, rather than mixing a server speech-stop event
with remote playback. Stratify by observed canonical path, including
`visual_insufficient` and `visual_unavailable`. Publish p50/p95 for fresh inference too;
do not silently relax the existing global SLO to accommodate it. The existing internal
thresholds come from `session_targets.json` version 5; this plan changes no runtime
target. Applying the same numeric values to participant-observed E2E latency is a proposed
additional Phase 5 gate, not a claim that the current scorecard measures that boundary.
Use the existing `summarize` empirical quantile convention and disclose it in the report.
Timeouts and missing answers are censored/missing observations with explicit counts,
never fast zero-latency samples. Report completion by the configured response deadline.

The proposed percentages are empirical development gates, not statistical proof of a
production error bound. Five meetings contain correlated turns. Publish sample counts,
per-meeting rates and ranges, and failed examples; do not claim an IID confidence bound
or general vision quality from this population. If eligible samples are insufficient,
report the corresponding gate as inconclusive rather than passed.

## 8. Cost and reliability accounting

Report question-triggered vision invocations per intended turn, their distribution and
maximum, and total vision invocations including background screen reading. Separate
cache reuse events / (reuse + inference acquisition events) from provider invocation counts:
acquisition events can contain retries, and a cache hit is not a paid provider call.
Retain all attempts, including ones superseded before publication.

Count authentication rejections, timeouts, parsing failures, transport/provider failures,
retry/fallback attempts, cancellations, and successful inferences separately. Use existing
fault names where sufficient; a measurement category does not create a new runtime taxonomy.
Missing failure details are reported as unknown, never relabelled as successful inference.

Meeting cost includes all enabled paid roles: vision and background vision, judge/reply,
STT, TTS, embeddings/memory calls, and any fallback. Split costs by role and provider,
per meeting and per intended turn; disclose excluded fixed infrastructure costs separately.
If a role cannot be observed or cost-bounded, full meeting cost is unknown and paid-run
readiness fails. Record provider/model/configuration, pricing source and effective date,
usage units, currency, and whether the amount is estimated or billing-reconciled.
Verify rates from official provider sources before approving a run; do not reuse #194's
price assumptions as current prices for every role.

Known recorded usage is a cost lower bound when a timed-out request might be billable.
Keep an explicit unknown charge and reserved conservative upper bound until reconciled.
An unknown cost is not US$0. Reserve request capacity and worst-case cost before outbound
work, including automatic SDK retries/fallbacks and concurrent requests. Uncertain or
unbounded estimates fail closed. A crash must not release a possibly spent reservation.

## 9. Separate approval for paid execution

Before any paid call, a new explicit approval must specify all of:

- New experiment ID and a new ledger path; #194's ledger remains read-only.
- Approved commit/configuration, provider/model choices, and paid roles enabled.
- Total outbound request cap and any role/provider allocations, retry/fallback limits,
  maximum concurrent work, and how streaming STT/TTS invocations consume capacity.
- Total USD cap covering all roles, a bounded reservation estimate per invocation,
  pricing references, and an upper bound for unresolved charges. Bound input/output tokens
  and audio/stream duration so a cost reservation has a finite justified upper bound.
- Approved meeting count/duration, pilot versus confirmatory scope, participants,
  and the exact scenario population the approved budget can fund.
- Stop behavior: no new outbound work at either cap; stop/disconnect cleanly and preserve
  in-flight reservations, recordings, failures, and partial-run status.

This draft proposes no new numeric request or USD allowance. A budget too small for the
specified population produces a separately labelled pilot or an incomplete run; it does
not authorize extra calls. A rate limit or exhausted quota ends the run for review.

## 10. Artifacts, comparisons, and completion decisions

Preserve an immutable run manifest with code/configuration/lock and scenario digests;
private participant-side video/audio and sampled frames; journals; claim-to-source and
sentence-to-attempt joins; independent labels and disagreement records; provider ledger;
cost bounds; raw per-turn timings; and a report with all numerators and denominators.
Keep private recordings out of the public repository. Public reports use sanitized IDs,
artifact hashes, and approved examples. Raw artifacts remain available to authorized reviewers.

The #194 **strict 5/15 grounding and 8/8 rubric violations** remain historical closed-rubric
results, not actual vision-quality estimates. Keep them unchanged beside, never merged
with, new human-adjudicated live metrics. No alias is added by this plan.
The pre-Phase-4 archived metric snapshot also stays unchanged: its target version 3,
historical version-2 fallback, path grouping, publication boundary, population, and missing
values differ from current measurement. Only compare numbers after explaining those
differences; do not claim improvement from evaluator changes.

Before implementation: approve the claim rubric, scenario population, publication
boundaries, proposed thresholds, telemetry gaps, and artifact contract in this draft.
After offline implementation: require deterministic contract/fault tests and complete
join validation before requesting a paid budget. After any approved live run: separate
measurement completeness from product quality. Missing telemetry means the measurement
is incomplete; a complete measurement with bad answers is a valid failing result.
Do not tune thresholds, aliases, or the confirmatory population after seeing its outputs.

`provider_unavailable` taxonomy and cassette timing brittleness remain separate debt.
Bring either into a measurement PR only with a concrete failed offline measurement check
showing it blocks the required observation; propose a separately reviewable minimal fix.
