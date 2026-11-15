# Writer spike protocol — libpulse asynchronous stream (W-A)

Status: **PRE-REGISTERED PROTOCOL — NOT RUN.** Committed before any trial so that
thresholds, counts and verdict rules cannot follow the results.

Parent: `2026-11-15-architecture-decision-review.md`, ADR-2. Authorized scope:
an offline experiment in the audio container against the real PulseAudio daemon.
No meeting, no participant, no browser, no provider request, US$0. The spike
produces evidence for a design decision. It ships no production code and cannot
by itself lift V6.

## Questions

| # | Question | Kind |
| --- | --- | --- |
| P1 | Does a write ever block, or is a short buffer reported? | Mechanism |
| P2 | Which of the four output events can be recorded, and is each an observation or an estimate? | Evidence |
| P3 | How long from final evaluation to output on an idle, primed stream? | Performance |
| P4 | After the deadline, can the writer make sink consumption of that frame impossible? | **Safety — decisive** |
| P5 | After cancellation or revocation, does the writer's account of samples out match what left? | Accounting |
| P6 | Do restart, zero-byte clips and clock faults yield failure or `unproven`, never a fabricated instant? | Honesty |

**Prediction recorded before running:** P4 fails under a stalled daemon. The
stop request is an asynchronous command to a process that also owns the render
thread; no ordering guarantee is known. The spike is run to confirm or refute
this, and a refutation needs a mechanism, not only clean trials.

## Setup

| Item | Fixed value |
| --- | --- |
| Environment | `docker/audio` image, daemon and `default.pa` unmodified; image digest, PulseAudio version, kernel and CPU count recorded in the manifest |
| Sink | `agent_output`, 24 kHz, mono, s16le, as in production |
| Writer under test | libpulse asynchronous stream on a threaded mainloop, corked start, 20 ms frames |
| Independent observer | A separate process recording `agent_output.monitor`, timestamping each fragment on `CLOCK_MONOTONIC`; it shares no state with the writer |
| Clip content | Each clip carries a unique sample pattern so the observer identifies clip and offset from the audio alone |
| Clock | One host, `CLOCK_MONOTONIC` for every process. Observer uncertainty is its fragment duration, measured and reported; it is added to every bound, never subtracted |
| Code location | `evals/writer_spike/`, outside `src/`; a direct binding for the spike only, no dependency added to the product |

Evidence rules: server indices taken from timing snapshots are observations of
the snapshot, timestamped at local receipt, so they bound the event from above.
Interpolated time and latency are estimates and are recorded only as such. A
field that cannot be observed is reported `unproven` and counted.

## Conditions and trials

| Condition | Injection | Trials |
| --- | --- | --- |
| C0 Baseline | Idle primed stream | 10,000 clips |
| C1 CPU contention | All cores saturated by competing processes; separately, a thread holding the interpreter lock in the writer process | 1,000 each |
| C2 Back-pressure | Stream buffer full; sink suspended | 1,000 each |
| C3 Delayed callback | Mainloop thread blocked for 50, 250 and 1,000 ms around the commit | 1,000 each |
| C4 Stalled server | Daemon stopped after handoff, resumed 50, 300 and 1,000 ms later | 1,000 each |
| C5 Server restart | Daemon killed and restarted at idle and mid-clip | 200 each |
| C6 Cancellation race | Cancel at uniformly random offsets from before commit to 200 ms after | 2,000 |
| C7 Revocation | Authorization withdrawn in the same window | 1,000 |
| C8 Expired deadline | Deadline already passed at handoff | 1,000 |
| C9 Zero-byte clip | Empty PCM | 200 |
| C10 Clock fault | Instant missing; instant from another clock | 200 each |

## Pass criteria

| # | Criterion | Applies to | Status |
| --- | --- | --- | --- |
| K1 | No write call exceeds 5 ms; a short buffer is reported | C0–C4, C8 | Experiment target |
| K2 | Expired write refused within 5 ms with zero samples observed | C8 | Experiment target |
| K3 | Final evaluation → first observed output p99.9 ≤ 100 ms | C0 | Experiment target |
| K4 | No clip is classed as written within bound unless the observer saw its first sample within 250 ms of the evaluation, uncertainty included | all | Invariant |
| K5 | **Zero samples of a frame observed after its deadline** | C1–C4, C8 | Invariant; one occurrence fails W-A for V6 |
| K6 | Zero samples observed that start after cancellation or revocation took effect in the writer | C6, C7 | Invariant |
| K7 | Samples the writer reports as out equal samples observed, within one frame | C6, C7 | Invariant |
| K8 | No instant recorded for an event that did not occur; restart reported as failure | C5, C9, C10 | Invariant |
| K9 | `evaluated ≤ enqueued ≤ server accepted ≤ sink consumed ≤ observed` wherever all are present | all | Invariant |

## Verdict rules

1. Every trial is reported. No trial is excluded, repeated or relabelled after
   the fact. A crashed trial counts as a failure of its condition.
2. An invariant fails on one occurrence. Experiment targets are reported with
   their full distributions.
3. Thresholds and counts are not edited after the first trial. A change means a
   new protocol revision and a new run.
4. Passing K5 in every trial does **not** establish prevention. W-A is put
   forward for V6 only if the report also contains a design argument that
   identifies the mechanism and shows why late consumption cannot occur, and
   that argument survives review.
5. If K5 fails, the report states the measured exposure (how many samples, how
   late) as a finding. It is not presented as a mitigated risk or a compliant
   result.
6. A FIFO spike follows only if the report identifies a boundary where the
   consuming stage enforces the deadline.

## Outputs

- Raw trial rows as JSONL with SHA-256, environment manifest, and the spike code
  at the commit that produced them.
- A report giving each criterion its result, distributions for the targets,
  every invariant violation with its trace, the list of `unproven` fields, and
  the design argument or the statement that none was found.

## What this spike cannot establish

Participant audibility; behavior inside a browser or a real meeting; a
deterministic guarantee from trial counts; production readiness of any binding.
