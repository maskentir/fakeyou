# Writer spike protocol — libpulse asynchronous stream

Status: **PROTOCOL REVISION 4 — UNDER REVIEW. NOT AUTHORIZED TO RUN. NO TRIAL HAS RUN.**

| Revision | Head | Outcome |
| --- | --- | --- |
| 1 | `3d9c784c` | Rejected: ordering invariant contradicted corked upload; PCM staged before authorization; client-updated indices used as server proof |
| 2 | `b2713b85` | Changes requested: write-return time used as a causal lower bound; revocation after commit held to a precommit criterion; playback-started used as per-clip acknowledgment; absence of output and delivery delay claimed without a bound; thread boundary of the commit section unspecified |
| 3 | `747506ec` | Six items closed. Changes requested: mainloop iterations inside the commit section contradicted the zero-turn criterion; the post-stop bound ignored audio already read downstream; preflight stop rule ambiguous for PF3 and PF4 |
| 4 | this file | Closes those three. Targets, trial counts and conditions are unchanged; what changes is where pumping happens and how results are interpreted |

Unchanged from the rulings: 5 ms refusal, p99.9 ≤ 100 ms, V1 250 ms, and the
trial counts of C0–C10.

Parent: `2026-11-15-architecture-decision-review.md`, ADR-2. Scope once
authorized: an offline experiment in the audio container against the real
PulseAudio daemon. No meeting, no participant, no browser, no provider request,
US$0. The spike produces evidence for a design decision. It ships no production
code and cannot by itself lift V6.

## Questions

| # | Question | Kind |
| --- | --- | --- |
| P1 | Does a write ever block, or is a short buffer reported? | Mechanism |
| P2 | What can be shown about server processing of a write, and how wide is the interval on observed output? | Evidence |
| P3 | How long from final evaluation to observed output on an idle stream? | Performance |
| P4 | When the first sample cannot meet its deadline, can the writer make any output of that clip impossible? | **Safety — decisive** |
| P5 | Can any sample be heard before, or without, a valid final evaluation? | **Safety — decisive** |
| P6 | After a stop, is the writer's account consistent with what was observed? | Accounting |
| P7 | Do restart, zero-byte clips and clock faults yield failure or `unproven`, never a fabricated instant? | Honesty |

**Prediction recorded before running:** P4 fails under a stalled daemon for both
arms. P5 holds for arm R by construction and cannot be proven for arm S. These
are hypotheses.

## Arms

PCM zones are defined in ADR-2: Z0 worker memory, Z1 staged in the server
without permission to play, Z2 released after a valid final evaluation.

| Arm | Design | PCM path | Standing |
| --- | --- | --- | --- |
| R | Release is the write. Stream running and empty, no prebuffer; the first 20 ms frame is written inside the commit section | Z0 → Z2 | Candidate |
| S | Stage, then release. First frame written to a corked stream; the commit section issues the uncork | Z0 → Z1 → Z2 | Characterization only. Outside I7 as written; no result from this arm can be put forward for V6 |

## Execution context and the commit section

One thread owns everything: the event loop thread runs the final evaluation and
every libpulse call. libpulse is driven by non-blocking iterations of a plain
mainloop on that thread. There is no threaded mainloop, no lock, and no hop to
another thread anywhere between evaluation and write.

A threaded mainloop is rejected for this spike: the caller would wait for the
mainloop lock, and the bytes would be sent to the server later by the mainloop
thread, which is an asynchronous handoff after the evaluation with no proven
bound.

| Step | Phase | May wait | Mainloop iteration |
| --- | --- | --- | --- |
| 1. Readiness | Before the section | Yes. Stream ready and not corked (arm R), writable size ≥ one frame, no data pending on the connection | Yes |
| 2. Readiness re-check | Commit section | No. Synchronous query only; if it fails the section is abandoned and step 1 repeats | **None** |
| 3. Final evaluation | Commit section | No | **None** |
| 4. Release | Commit section | No. `release_invoked_at`, the write (arm R) or uncork request (arm S), `release_returned_at` | **None** |
| 5. Pump | Postcommit | Not applicable | Yes. `first_pump_invoked_at`; non-blocking iterations until nothing is pending on the connection, then `sent_at`; `send_incomplete` if data is still pending after the first pump |

The commit section is steps 2 to 4: one synchronous function that makes no
mainloop iteration. A mainloop iteration prepares, polls and dispatches, and
dispatch runs callbacks. Keeping every iteration outside the section means no
libpulse callback can run between the evaluation and the write, so nothing can
re-enter and change state the evaluation read. The claim is that specific one,
not atomicity from being on one thread.

The readiness re-check comes before the evaluation so that the evaluation is the
last decision before the write. Its answer reflects the last iteration before
the section and may be stale; a write that then finds no room is refused and
recorded. If readiness is lost, the evaluation is not reused; it is made again
after the next wait.

**The handoff in the section is to the client library, not to the server.** When
the write returns, the frame is queued in the worker process. It reaches the
server only when step 5 runs. A delay between steps 4 and 5 is therefore a delay
between the commit and the bytes leaving the process, inside the client. C3
injects exactly that. Whether the frame reached the server, and when, stays a
question for the trials.

What this does not claim: a writable-size check says the client may queue a
frame. It says nothing about the path downstream of the socket, or about output
meeting a deadline.

## Preflight

Preflight is part of the spike and runs only once the protocol is authorized.
Each item below is an assumption of this protocol, not an established fact. The
stop and go rule for each is fixed in the table after this one.

| # | Assumption | Established by |
| --- | --- | --- |
| PF1 | In this context the write call queues and returns without blocking, including with a full buffer and a stopped daemon | Test |
| PF2 | Bytes leave the process only during iterations made by the owning thread, and the pending-data query reflects unsent data | Reading the client library source for the version in the image, plus a test with the socket observed externally |
| PF3 | The reply to a timing request issued after a write cannot be produced before the server has processed that write | A written argument from the daemon source for the version in the image, naming the code path. Trials cannot establish this |
| PF4a | Once the server no longer lists a stream, nothing further can be read from that stream's buffer | Written argument from the daemon source for the version in the image, plus a test |
| PF4b | PCM of that stream already read into the sink and monitor path is gone, or leaves within a stated bound, once the stream is removed | Written argument from the daemon source covering the downstream path. A test and an observation window can corroborate it; they cannot establish it |
| PF5 | Configured latency of `agent_output` and its monitor | Read from the running server and recorded in the manifest |
| PF6 | The daemon can be confirmed stopped: every thread reported stopped by the kernel before the next step proceeds | Test |
| PF7 | The observer attributes samples to clips with no false match on silence, on other clips, or on partial fragments | 100,000 clip patterns against recorded silence and each other |
| PF8 | No other client or module can uncork, move or feed the streams under test | Module list and client list recorded; test |
| PF9 | The calls made inside the commit section (writable-size query, write, uncork request) invoke no callback before returning | Reading the client library source, plus a test with counting callbacks registered on every hook |

| Preflight | If not established | Trials affected |
| --- | --- | --- |
| PF1, PF7, PF8, PF9 | **Stop.** No trial runs. Protocol revision required | All |
| PF2 | The trials that depend on it do not run | C3; every use of `first_pump_invoked_at` and `send_incomplete` |
| PF5 | The trials that depend on it do not run | Every trial with an observation window |
| PF6 | The trials that depend on it do not run | C4, C5, C12; every bound raised to `stall_until` |
| PF3 | Trials run. Server processing is reported `unproven` for the whole run | None; no criterion depends on it |
| PF4a, PF4b | Trials run and are recorded. Route N2 is never reported as proven; every trial that would need it is `unproven` | Arm S no-output claims; arm R clips withheld after release |

A condition that does not run is reported as not run, with the failed preflight
as the reason. It is not reported as passed, and the spike is not stretched to
run it anyway.

## Setup

| Item | Fixed value |
| --- | --- |
| Environment | `docker/audio` image, daemon and `default.pa` unmodified; image digest, PulseAudio version, kernel and CPU count in the manifest |
| Sink | `agent_output`, 24 kHz, mono, s16le, as in production |
| Streams | C0–C3 and C10: one persistent stream, as in production. Every condition with a no-output claim: one stream per trial, identified by its server index |
| Independent observer | A separate process recording `agent_output.monitor`, stamping each fragment at receipt. It shares no state with the writer. It reads from the same daemon, which limits what it can prove (see "No output") |
| Control connection | A third process that queries the server for stream existence. It is not the writer's connection |
| Clip content | A unique sample pattern per clip, so the observer identifies clip and sample offset from audio alone |
| Clock | One host, `CLOCK_MONOTONIC` in every process |
| Final evaluation | A deterministic stub returning a scheduled `valid`, `invalid` or `unknown` |
| Order counter | One counter incremented only on the owning thread, stamped on the commit section and on every applied revocation or stop |
| Code | `evals/writer_spike/`, outside `src/`; a direct binding for the spike only, no product dependency |

## Recorded per trial

No field is named or mapped to the contract's `accepted_at` or
`first_written_at`.

| Phase | Field | Meaning |
| --- | --- | --- |
| Precommit, arm S | `stage_invoked_at`, `stage_returned_at` | Around the staging write. `stage_invoked_at` is the first instant any PCM of the clip was handed to libpulse |
| | `stage_sent_at` | Nothing pending on the connection after staging |
| Commit | `evaluated_at`, `commit_seq` | Final evaluation; order counter value of the section |
| | `release_invoked_at`, `release_returned_at` | Immediately before and after the release call |
| | `app_turns_in_section` | Event-loop task switches between steps 2 and 4; must be 0 |
| | `pulse_iterations_in_section`, `callbacks_in_section` | libpulse mainloop iterations and callbacks invoked between steps 2 and 4; both must be 0 |
| Postcommit | `first_pump_invoked_at` | Start of the first mainloop iteration after the section |
| | `sent_at` or `send_incomplete` | Step 5 |
| | `ordered_reply_at` | Receipt of the reply to a timing request issued after the release |
| | `buffer_read_at` | Receipt of a timing snapshot requested after the release, not flagged corrupt, read index past the clip start |
| | `started_notified_at` | Playback-started notification. **Diagnostic only** |
| | `output_interval` | From the observer, defined below |
| Stop | `stop_requested_at`, `stop_applied_at`, `stop_seq` | Request by the caller; application on the owning thread; order counter value |
| | `handoff_log` | Every release call for the clip with byte count and order counter value |
| | `released_before_stop_bytes` | From `handoff_log` |
| | `unread_stream_bytes` | Released bytes not yet read from the stream buffer, from a valid snapshot taken at or after the stop; otherwise `unknown` |
| | `downstream_inflight_bytes` | Bytes read from the stream buffer and not proven output. `unknown` unless PF4b supplies a bound |
| | `observed_post_stop_bytes` | Bytes of the clip in observer fragments received after `stop_applied_at`. Some may have been output before the stop and received later |
| Closure | `teardown_confirmed_at`, `observed_until` | Control connection no longer lists the stream; end of the observation window |
| Fault | `stall_confirmed_at`, `stall_until` | Daemon confirmed stopped (PF6); resume signal sent |

### What each signal shows

Three different facts, never substituted for one another.

| Fact | Signal | Limits |
| --- | --- | --- |
| The server processed this write | `ordered_reply_at`, only if PF3 is established. The proof is the ordering argument together with the local fact that the write preceded the request on the same stream. The reply alone proves nothing about a write | A late reply is not a false reply. Callbacks can be delayed; no timing test accepts or rejects this signal |
| When that happened | Nothing gives an instant. It lies in `[release_invoked_at, ordered_reply_at]` | Reported as an interval |
| A sample reached output | The observer only | See below |

- The local `write_index` is recorded and is evidence of nothing.
- `started_notified_at` reports that playback began after an underrun or at
  start-up. It is not an acknowledgment of any frame and is used in no criterion
  and no bound.
- `buffer_read_at` shows the stream buffer was read. It is not sink output and is
  used in no timing criterion.
- One falsification check applies to `ordered_reply_at`: a reply received while
  the daemon is confirmed stopped, for a request issued after the stop, means the
  signal is not server-originated and it is discarded for the whole report. This
  can refute the signal. It cannot validate it.

### Output interval

Only deterministic bounds are used in classification.

| Bound | Value | Why it is a bound |
| --- | --- | --- |
| Upper, `out_hi` | Receipt instant of the observer fragment holding the sample | A sample cannot be recorded before it was output. Scheduling and transport delay only move this later |
| Lower, `out_lo`, arm R | `release_invoked_at`, provided `handoff_log` shows no earlier handoff of the clip. Raised to `first_pump_invoked_at` when PF2 is established. Raised to `stall_until` when `stall_confirmed_at ≤ release_invoked_at` | No PCM of the clip existed outside the worker before the call began; with PF2, none left the process before the first pump. A stopped daemon renders nothing |
| Lower, `out_lo`, arm S | `stage_invoked_at`, raised to `stall_until` under the same stall rule | The uncork instant is **not** a lower bound: whether staged PCM can play before it is the property under test |

The write-return instant is never a lower bound: the server can process a frame
before the call returns.

The observer's delivery delay `U` is measured in a calibration run and reported
as a distribution. It is an empirical figure. It is used in no bound and no
classification.

### Classification against a deadline `d`

| Result | Condition |
| --- | --- |
| Within bound, proven | `out_hi ≤ d` |
| Confirmed violation | `out_lo > d` |
| Unproven | `out_lo ≤ d < out_hi` |

Errors run one way: a sample truly within bound may be classed unproven; a
sample truly late is never classed within bound. Late output can be *confirmed*
only where a lower bound moves past the deadline: a daemon stall (C4), or, with
PF2, a pump delayed past the deadline (C3). Elsewhere it is unproven. That is a
limit of the instrument and is reported as one.

### No output

"No output, proven" has two routes. Anything else is `unproven`.

| Route | Applies when | Requirements |
| --- | --- | --- |
| N1 Never handed over | Arm R, clip not released | `handoff_log` is empty for the clip. This is a fact about the worker and does not depend on the observer. The observer window below is run as corroboration; a sample seen there is a K7 failure |
| N2 Handed over, then destroyed | PCM was staged or released | All of: the stream is the trial's own; discard and disconnect were requested; the control connection confirms the server no longer lists the stream; PF4a and PF4b are both established; no sample of the clip is observed from the first handoff until `observed_until`; a sentinel clip on a new stream is observed after `observed_until` |

Observation window: `observed_until` is `teardown_confirmed_at` plus the larger
of 2 s and ten times the latency recorded in PF5.

N2 rests on the two arguments, not on the silence. A stream missing from the
server's list shows that reading from its buffer has ended (PF4a). It does not
show that audio already read into the sink and monitor path is gone; that needs
PF4b. The observation window is an empirical observation: silence followed by a
sentinel shows the observer was alive and saw nothing in that window, not that
old PCM could never appear later. If PF4b gives a bound longer than the window,
the window is extended to that bound before any trial.

Where either argument is missing, teardown cannot be confirmed (daemon stopped
or killed, control connection failed), or the daemon restarted inside the
window, the trial is `unproven`.

## Deadlines

- **First frame.** `d = evaluated_at + 250 ms` in this spike; the freshness term
  of the production deadline is held constant by the stub and reported. V1
  applies to the first sample only.
- **Whole clip, when the first sample missed.** No sample of the clip may be
  output at any time (V2, V3).
- **Continuation.** Frames after a first sample that met its deadline have no
  deadline in #205. Their timing is recorded as information; no criterion
  applies to them and none is introduced here.

## Conditions and trials

| Condition | Injection | Arm R | Arm S |
| --- | --- | --- | --- |
| C0 Baseline | Idle stream, evaluation `valid` | 10,000 clips | 2,000 |
| C1 CPU contention | All cores saturated; separately, a second thread holding the interpreter lock | 1,000 each | — |
| C2 Back-pressure | Stream buffer full; sink suspended | 1,000 each | — |
| C3 Delayed dispatch | Owning thread blocked 50, 250, 1,000 ms: between readiness and the section; and right after the section, before any further iteration | 1,000 each | 1,000 each |
| C4 Stalled server | Daemon confirmed stopped **before** the release, resumed 50, 300, 1,000 ms later; and stopped just after the release | 1,000 each | 1,000 each |
| C5 Server restart | Daemon killed and restarted at idle and mid-clip | 200 each | 200 each |
| C6a Precommit cancel | Cancel applied before the commit section, at random earlier instants | 1,000 | 1,000 |
| C6b Postcommit stop | Stop requested 0 to 200 ms after the release | 2,000 | — |
| C7 Revocation near commit | Revocation requested from another thread within ±5 ms of the section. Each trial is classed afterwards from the order counter, not from the offset | 1,000 | 1,000 |
| C8 Expired deadline | Deadline already passed at the section | 1,000 | 1,000 |
| C9 Zero-byte clip | Empty PCM | 200 | — |
| C10 Clock fault | Instant missing; instant from another clock | 200 each | — |
| C11 Not valid | Final evaluation `invalid` or `unknown` | 1,000 | 1,000 |
| C12 Restart while staged | Daemon killed and restarted between staging and evaluation | — | 200 |
| C13 Release ordering | Owning thread blocked between staging and evaluation; evaluation `invalid`; discard and a following valid clip issued back to back | — | 1,000 |

**C7 classes.** A revocation takes effect when the owning thread applies it,
which is the order the final evaluation can see.

| Class | Record | Judged by |
| --- | --- | --- |
| C7a Before commit | `stop_seq < commit_seq` | K6: zero handoff, no output proven |
| C7b After commit | `stop_seq > commit_seq` | K8: postcommit stop. The commit stands |
| C7c Undetermined | Either counter value missing, or the record cannot order them | `unproven`; counted |

A revocation requested before the section and applied after it is class C7b.
Its request-to-application delay is reported; it is not relabelled as precommit.
A class with fewer than 100 trials is reported `unproven` for that class.

## Criteria

| # | Criterion | Conditions | Status |
| --- | --- | --- | --- |
| K1 | No release call exceeds 5 ms from invocation to return; a short buffer is reported, not waited on | C0–C4, C8 | Experiment target |
| K2 | An expired first frame is refused within 5 ms, and no output is proven by route N1 | C8 | Target for the 5 ms; invariant for the output |
| K3 | `out_hi − evaluated_at` p99.9 ≤ 100 ms | C0 | Experiment target |
| K4 | No clip is reported as written within bound unless its first sample is within bound, proven | all | Invariant |
| K5 | For a clip whose first sample was not proven within bound: no confirmed violation | C1–C4, C8 | Invariant; one occurrence fails the arm for V6 |
| K6 | Cancel or revocation applied before commit, or an evaluation that is not `valid`: empty `handoff_log` in arm R, and no output proven | C6a, C7a, C11–C13 | Invariant |
| K7 | Arm R: no sample with `out_hi < release_invoked_at`. An occurrence means a handoff outside the commit section or a faulty instrument; the run's observer evidence is void until explained. Arm S: a sample with `out_hi < release_invoked_at` is confirmed output before authorization | all | Invariant |
| K8a | Postcommit stop: `handoff_log` shows no release with an order value after `stop_seq` | C6b, C7b | Invariant; a fact about the worker |
| K8b | The receipt states audio was written exactly when a sample of the clip was observed, and the bytes observed in total do not exceed `released_before_stop_bytes` | C6b, C7b | Invariant |
| K8c | Bound on output after the stop | C6b, C7b | No bound is asserted. `observed_post_stop_bytes` is reported as a distribution beside `unread_stream_bytes`. The verdict is `unproven` unless PF4b supplies a downstream bound, in which case the bound checked is `unread_stream_bytes` plus that bound |
| K9 | Ordering. Arm R: `evaluated ≤ release_invoked ≤ release_returned`; `ordered_reply`, `buffer_read` and `out_hi` are not before `release_invoked`. Arm S: `stage_invoked ≤ stage_returned ≤ evaluated ≤ release_invoked ≤ release_returned`; the same signals are not before `stage_invoked` | all | Invariant |
| K10 | No instant recorded for an event that did not occur; a restart is reported as failure; a missing or foreign-clock instant yields `unproven` | C5, C9, C10 | Invariant |
| K11 | `app_turns_in_section`, `pulse_iterations_in_section` and `callbacks_in_section` are each 0 in every trial | all | Invariant |
| K12 | `first_pump_invoked_at − release_returned_at` and `sent_at − release_returned_at` are reported as distributions; `send_incomplete` trials are counted and listed | all | Report |

K8 does not claim a rollback and does not claim a limit on what is heard after
a stop. The committed decision stands. Audio already read from the stream buffer
may be in the sink or monitor path when the stop is applied; it is not counted
as unread, and no tolerance is invented to cover it.

In arm S a valid trial cannot show that its audio began only after the uncork:
`out_lo` is the staging instant, which precedes it. Authorization order in arm S
is therefore `unproven` in every valid trial, by design of the measurement.

## Verdict rules

1. Every trial is reported. None is excluded, repeated or relabelled afterwards.
   A crashed trial counts as a failure of its condition.
2. An invariant fails on one confirmed occurrence. Targets are reported with
   full distributions.
3. Unproven trials never support a pass. A criterion passes only if every trial
   in scope is proven; otherwise its result is `unproven` with the count.
4. Thresholds, counts, bounds and classification rules are not edited after the
   first trial. A change is a new protocol revision and a new run.
5. No confirmed violation of K5 does **not** establish prevention. Arm R is put
   forward for V6 only with a design argument that names the mechanism making
   late output impossible, and only if that argument survives review.
6. If K5 fails, the report states the measured exposure, samples and lateness,
   as a finding. It is not presented as a mitigated risk, and no guard-band
   variant is proposed in its place.
7. Arm S results are labelled characterization throughout.
8. A FIFO spike follows only if the report identifies a boundary where the
   consuming stage enforces the deadline.

## Outputs

- Raw trial rows as JSONL with SHA-256, the environment manifest, preflight
  results and arguments, calibration runs, and the spike code at the commit that
  produced them.
- A report: each criterion's result; distributions for targets; every confirmed
  violation with its trace; every unproven trial with the reason; the C7 class
  counts; whether PF3, PF4a and PF4b were established; the latency difference between
  arms; and the design argument, or the statement that none was found.

## What this spike cannot establish

Participant audibility; behavior in a browser or a real meeting; a deterministic
guarantee from trial counts; the instant at which the server processed a write;
which event satisfies `first_written_at`; production readiness of any binding.
