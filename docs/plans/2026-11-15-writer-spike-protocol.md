# Writer spike protocol — libpulse asynchronous stream

Status: **PROTOCOL REVISION 2 — UNDER REVIEW. NOT AUTHORIZED TO RUN. NO TRIAL HAS RUN.**

Revision 1 (`3d9c784c`) was rejected in review: its ordering invariant
contradicted the corked-upload design, it let PCM cross the process boundary
before authorization, and it treated client-updated indices as server proof.
This revision replaces it. Thresholds and counts ruled earlier are unchanged.

Parent: `2026-11-15-architecture-decision-review.md`, ADR-2. Scope once
authorized: an offline experiment in the audio container against the real
PulseAudio daemon. No meeting, no participant, no browser, no provider request,
US$0. The spike produces evidence for a design decision. It ships no production
code and cannot by itself lift V6.

## Questions

| # | Question | Kind |
| --- | --- | --- |
| P1 | Does a write ever block, or is a short buffer reported? | Mechanism |
| P2 | Which signals are valid evidence of server acceptance and of buffer read, and how wide is the uncertainty on observed output? | Evidence |
| P3 | How long from final evaluation to observed output on an idle stream? | Performance |
| P4 | When the first sample cannot meet its deadline, can the writer make any output of that clip impossible? | **Safety — decisive** |
| P5 | Can any sample be heard before, or without, a valid final evaluation? | **Safety — decisive** |
| P6 | After a stop, is the writer's account of what left consistent with what was observed? | Accounting |
| P7 | Do restart, zero-byte clips and clock faults yield failure or `unproven`, never a fabricated instant? | Honesty |

**Prediction recorded before running:** P4 fails under a stalled daemon for both
arms, because the stop is an asynchronous command to the process that owns the
render thread. P5 is expected to hold for arm R by construction and to be
unprovable for arm S. These are hypotheses.

## Arms

PCM zones are defined in ADR-2: Z0 worker memory, Z1 staged in the server
without permission to play, Z2 released after a valid final evaluation.

| Arm | Design | PCM path | Standing |
| --- | --- | --- | --- |
| R | Release is the write (W-A2). Stream running and empty, no prebuffer; the commit section is final evaluation then a non-blocking write of the first 20 ms frame after a writable-size check | Z0 → Z2 | Candidate |
| S | Stage, then release (W-A1). First frame uploaded to a corked stream; the commit section is final evaluation then an uncork request | Z0 → Z1 → Z2 | Characterization only. Outside I7 as written; no result from this arm can be put forward for V6 |

## Setup

| Item | Fixed value |
| --- | --- |
| Environment | `docker/audio` image, daemon and `default.pa` unmodified; image digest, PulseAudio version, kernel and CPU count in the manifest |
| Sink | `agent_output`, 24 kHz, mono, s16le, as in production |
| Independent observer | A separate process recording `agent_output.monitor`, stamping each fragment at receipt on `CLOCK_MONOTONIC`. It shares no state with the writer |
| Clip content | Each clip carries a unique sample pattern, so the observer identifies clip and sample offset from audio alone |
| Clock | One host, `CLOCK_MONOTONIC` in every process |
| Final evaluation | A deterministic stub returning a scheduled `valid`, `invalid` or `unknown`; the perception store is not part of this spike |
| Code | `evals/writer_spike/`, outside `src/`; a direct binding for the spike only, no product dependency |

## Recorded per trial

Precommit and postcommit instants are separate fields. None is named or mapped
to the contract's `accepted_at` or `first_written_at`.

| Phase | Field | Source |
| --- | --- | --- |
| Precommit (arm S only) | `staged_enqueued_at` | Write call returned |
| | `staged_ack_at` | Server-originated signal, as defined below |
| Commit | `evaluated_at` | Stub evaluation instant |
| | `released_at` | Arm R: first-frame write call returned. Arm S: uncork request issued |
| Postcommit | `ordered_reply_at` | Reply received to a command issued after the release on the same connection |
| | `started_notified_at` | Server playback-started notification received |
| | `buffer_read_at` | Receipt of a timing snapshot requested after the release, not flagged corrupt, with read index past the clip start |
| | `output_interval` | From the observer, as defined below |
| Stop | `stop_requested_at`, `stop_effective_at` | Request by the caller; instant after which the writer releases nothing |
| | `fill_at_stop` | PCM released and not yet shown read, in samples |
| Fault | `stall_from`, `stall_until` | Instants the daemon was stopped and resumed |

**Evidence rules.**

- The local `write_index` is recorded and is evidence of nothing. The client
  library advances it at the write call.
- `ordered_reply_at` and `started_notified_at` are *candidate* evidence of server
  acceptance. A candidate is validated only if, across all trials, it never
  occurs inside `[stall_from, stall_until)` for a release made during the stall,
  and never later than the observer's upper bound for a clip that was heard. A
  candidate that fails once is discarded for the whole report.
- `buffer_read_at` is evidence that the stream buffer was read. It is not
  evidence of sink output and is never used for K4 or K5.
- Interpolated time and latency are recorded as estimates only.

**Output interval.** A sample was output no later than the observer received its
fragment and no earlier than it was released.

| Bound | Value |
| --- | --- |
| Upper, `out_hi` | Receipt instant of the fragment holding the sample |
| Lower, `out_lo` | The latest of: `released_at`; `stall_until` when the release fell inside the stall; `out_hi − U` when the observer was not under fault |
| `U` | Fragment duration plus the observer's delivery delay bound, measured in a calibration run before each condition and reported. Under any fault that can delay the observer itself (C1, C4, C5), `U` is treated as unbounded and the third term is dropped |

**Classification against a deadline `d`.**

| Result | Condition |
| --- | --- |
| Within bound, proven | `out_hi ≤ d` |
| Confirmed violation | `out_lo > d` |
| Unproven | `out_lo ≤ d < out_hi` |
| No output, proven | No sample of the clip observed for the rest of the run, and the observer demonstrably alive (a sentinel clip is observed afterwards) |

An unproven trial is neither a pass nor a violation. It is counted and listed.

## Deadlines

- **First frame.** `d = evaluated_at + 250 ms` in this spike; the sample
  freshness term of the production deadline is fixed at a constant by the stub
  and reported. V1 applies to the first sample only.
- **Whole clip, when the first sample missed.** No sample of the clip may be
  output at any time (V2, V3).
- **Continuation.** Frames after a first sample that met its deadline have no
  deadline in #205. Their timing is recorded as information. No criterion
  below applies to them, and none is introduced here.

## Conditions and trials

| Condition | Injection | Arm R | Arm S |
| --- | --- | --- | --- |
| C0 Baseline | Idle stream, evaluation `valid` | 10,000 clips | 2,000 |
| C1 CPU contention | All cores saturated; separately, a thread holding the interpreter lock in the writer | 1,000 each | — |
| C2 Back-pressure | Stream buffer full; sink suspended | 1,000 each | — |
| C3 Delayed callback | Mainloop thread blocked 50, 250, 1,000 ms around the commit | 1,000 each | 1,000 each |
| C4 Stalled server | Daemon stopped **before** the release, resumed 50, 300, 1,000 ms later; and stopped just after the release | 1,000 each | 1,000 each |
| C5 Server restart | Daemon killed and restarted at idle and mid-clip | 200 each | 200 each |
| C6a Precommit cancel | Cancel or revoke at random instants before the commit | 1,000 | 1,000 |
| C6b Postcommit stop | Stop at random offsets from 0 to 200 ms after the release | 2,000 | — |
| C7 Revocation at commit | Authorization withdrawn within ±5 ms of the commit section | 1,000 | 1,000 |
| C8 Expired deadline | Deadline already passed at the commit section | 1,000 | 1,000 |
| C9 Zero-byte clip | Empty PCM | 200 | — |
| C10 Clock fault | Instant missing; instant from another clock | 200 each | — |
| C11 Not valid | Final evaluation `invalid` or `unknown` | 1,000 | 1,000 |
| C12 Restart while staged | Daemon killed and restarted between staging and evaluation | — | 200 |
| C13 Release ordering | Mainloop blocked between staging and evaluation; evaluation `invalid`; discard and a following valid clip issued back to back | — | 1,000 |

Stopping the daemon before the release (C4) makes `out_lo = stall_until`, so a
sample heard after a deadline that expired during the stall is a confirmed
violation, not an ambiguous one.

## Criteria

| # | Criterion | Conditions | Status |
| --- | --- | --- | --- |
| K1 | No write call exceeds 5 ms; a short buffer is reported, not waited on | C0–C4, C8 | Experiment target |
| K2 | An expired first frame is refused within 5 ms, and no output is proven | C8 | Target for the 5 ms; invariant for the output |
| K3 | `out_hi − evaluated_at` p99.9 ≤ 100 ms | C0 | Experiment target |
| K4 | No clip is reported as written within bound unless its first sample is within bound, proven | all | Invariant |
| K5 | For a clip whose first sample was not proven within bound: zero confirmed violations, that is, no sample with `out_lo > d` | C1–C4, C8 | Invariant; one occurrence fails the arm for V6 |
| K6 | Precommit cancel, revocation, or an evaluation that is not `valid`: no output, proven | C6a, C7, C11–C13 | Invariant |
| K7 | No sample observed with `out_hi < released_at` | all | Invariant |
| K8 | Postcommit stop: nothing released after `stop_effective_at`; samples observed after it do not exceed `fill_at_stop` plus one frame; the receipt states audio was written when any sample was observed | C6b | Invariant |
| K9 | Ordering. Arm R: `evaluated ≤ released`, and every validated postcommit signal and `out_hi` are at or after `released`. Arm S: `staged_enqueued ≤ staged_ack ≤ evaluated ≤ released`, with the same postcommit rule | all | Invariant |
| K10 | No instant recorded for an event that did not occur; a restart is reported as failure; a missing or foreign-clock instant yields `unproven` | C5, C9, C10 | Invariant |

K8 does not claim a rollback. The committed decision stands; the criterion is
that the stop releases nothing further and the account is truthful.

## Verdict rules

1. Every trial is reported. None is excluded, repeated or relabelled afterwards.
   A crashed trial counts as a failure of its condition.
2. An invariant fails on one confirmed occurrence. Targets are reported with
   full distributions.
3. Unproven trials never support a pass. A criterion passes only if every trial
   in scope is proven; otherwise its result is `unproven` with the count.
4. Thresholds, counts and classification rules are not edited after the first
   trial. A change is a new protocol revision and a new run.
5. Zero confirmed violations of K5 does **not** establish prevention. Arm R is
   put forward for V6 only with a design argument that names the mechanism
   making late output impossible, and only if that argument survives review.
6. If K5 fails, the report states the measured exposure, samples and lateness,
   as a finding. It is not presented as a mitigated risk.
7. Arm S results are labelled characterization throughout. A clean K6 in arm S
   is not a proof that staged audio cannot play.
8. A FIFO spike follows only if the report identifies a boundary where the
   consuming stage enforces the deadline.

## Outputs

- Raw trial rows as JSONL with SHA-256, the environment manifest, calibration
  runs, and the spike code at the commit that produced them.
- A report: each criterion's result; distributions for targets; every confirmed
  violation with its trace; every unproven trial with the reason; which
  server-acceptance candidates were validated or discarded; the latency
  difference between arms; and the design argument, or the statement that none
  was found.

## What this spike cannot establish

Participant audibility; behavior in a browser or a real meeting; a deterministic
guarantee from trial counts; which event satisfies `first_written_at`;
production readiness of any binding.
