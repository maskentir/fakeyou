# Architecture Decision Closure — ADR-2 physical writer

Status: **CLOSED BY CO-FOUNDER DECISION, 2026-11-15. #205 UNCHANGED. PublicationContext OFF. Phase 5B NO-GO.**

This closes ADR-2 of `2026-11-15-architecture-decision-review.md`. It records a
decision and the evidence it rests on. It proposes no design, revises no
protocol and changes no contract text. ADR-1 and ADR-3 are not closed here.

## Final disposition

| Item | Disposition |
| --- | --- |
| W-A2, placement relative to the read index | **Rejected for V6** |
| W-A2, placement at an absolute index 40 ms ahead | **Rejected for V6** |
| Placement relative to the write index, no prebuffer | Invalid baseline: produced no audio |
| W-A1, staging on a corked stream | Never a candidate; outside I7 as written. Not run |
| W-B, worker around the blocking API | Rejected for V6 earlier, by construction |
| Guard band; client-side write refusal | Not a safety boundary. Neither satisfies V2 |
| Full S1 trials | **Cancelled** for these configurations |
| Spike protocol revision 4 | **Superseded** by the diagnostic findings. Not revised |
| Current-scope visual speech | **Activation blocked** |
| Contract #205 | Unchanged |

No further variant of this writer is to be tried as a route to activation: no
other lead, prebuffer, guard band or retry.

## Why

V6 requires a writer that satisfies one branch of V2: prevent the output when
the first write would miss its allowance, or re-evaluate the binding
synchronously immediately before the output.

With either placement, once the PCM was handed over and the daemon stalled, the
client had no means of withdrawing it, and nothing on the server side
re-evaluated anything before rendering. The audio was heard after the
allowance. V4 lets such audio be reported truthfully; truthful reporting does
not satisfy V2.

## Evidence, by class

Source: `docs/reports/2026-11-15-writer-spike-preflight.md` and `.json`, harness
commit `eba185d7`, PulseAudio 17.0 in the unmodified audio container. Diagnostic
scale: 50 clips per cell, one host. The three classes below are kept apart and
none is promoted by this closure.

### Confirmed violation

| Configuration | Scenario | Result | Why it is confirmed |
| --- | --- | --- | --- |
| Read-index placement | Daemon confirmed stopped before the write, resumed about 500 ms later | 50 of 50 clips heard in full | A stopped daemon renders nothing, so no sample left before the resume instant, which was more than 250 ms after the write call. The bound is causal and does not depend on observer timing |
| Absolute index +40 ms | Same | 50 of 50 clips heard in full | Same |

### Observation only

| Observation | Result | Why it is not more |
| --- | --- | --- |
| Read-index placement, pump held 500 ms | 50 of 50 heard, received about 505 ms after the write call | Receipt is an upper bound on output time; the pump-based lower bound rests on PF2, which is unproven. Late output is not confirmed here |
| Absolute placement, pump held 500 ms | 0 of 50 observed | Not seeing a clip is not proof it was discarded. PF4a and PF4b are unproven |
| Stream teardown | 0 bytes received after confirmed teardown, 50 of 50 twice | Fifty repetitions; the downstream argument is unreviewed |
| Normal operation at the read index | 100 of 100 whole, median 0.6 ms from write call to receipt | Liveness, not safety |
| Relative write without prebuffer | 0 of 100 heard | Shows the baseline was invalid; says nothing about safety |
| Production-like buffer settings | 98 of 100 clips lost their beginning | The production `pa_simple` writer was not exercised. A lead for a separate investigation |

### Unproven

| Item | State |
| --- | --- |
| PF2: the PCM reference is published only during a mainloop iteration | Source argument written; shared ring not instrumented; not reviewed |
| PF3: a timing reply cannot precede processing of an earlier write | Source argument written; not refuted in 100 repetitions; not reviewed |
| PF4a, PF4b: nothing is read or remains downstream after stream removal | Source arguments written; not reviewed |

These stay unproven. The rejection does not need them: it rests on the
confirmed violations alone.

## Limits of this conclusion

- It rejects the designs tested. It does not show that every output
  architecture on PulseAudio fails, and none other was examined.
- A component that controls consumption and checks a wall-clock deadline at
  that boundary was not evaluated.
- The production `pa_simple` writer was not tested. It was already unable to
  meet V2 by construction (finding F4 of the review).
- No CPU-contention, restart or cancellation scenario was run.
- The confirmed cells come from one run on one host.

## Evidence custody

| Item | State |
| --- | --- |
| Results | Committed: `docs/reports/2026-11-15-writer-spike-preflight.json`, with SHA-256 of each raw file |
| Raw files | Observer recordings, relay journal, run log, source tarball. Archive `writer-spike-raw-eba185d7.tar.xz`, 3,088,788 bytes, SHA-256 `974bc6012f2a5c28ea2d9b264c3821948167b747ed05c15d18603f918faa4513` |
| Where the archive is | **On the author's machine only**, under `.runtime/writer-spike/`. A hash without the file does not allow replay; a location reachable by an authorized auditor has not been decided |
| Replay tool | `evals/writer_spike/replay.py`, no audio server needed: `python -m evals.writer_spike.replay <directory>` |

What a replay from the raw recordings can verify, and did when run by the
author (not an independent audit):

- which clips appear whole in each recording: 50, 50, 50, 50, 0, 50 across the
  six cells, matching the report;
- that each of the two daemon-stop recordings holds exactly 50 gaps longer than
  250 ms in observer delivery, and the other four hold none.

What it cannot verify: the instant of each write call and of each resume. The
harness kept those in memory and wrote only aggregates, so the per-clip
lateness cannot be recomputed from the raw files. This is a gap in the evidence
as recorded. It is consistent with the stall gaps above but is not a substitute
for them. Closing it would need a rerun that persists per-clip rows; none is
planned, since no further run of these configurations is authorized.

## What stays in force

- V6 stands unmet. As written, V6 blocks production activation when the writer
  satisfies neither branch of V2. Whether statements that carry no V1 bound
  (historical, abstention) could ever be activated on the existing writer is a
  contract question this closure does not answer.
- ADR-1 and ADR-3: direction accepted, nothing implemented. The NC-76 / I7
  amendment still needs its own contract review.
- Observation, transcript and decision capture do not depend on any of this.

## Condition on any future attempt

Recorded as a condition, not as a proposal. No such project is approved.

1. The deadline is enforced at a consumption boundary the runtime controls, not
   only by the producer.
2. The check uses a clock that keeps advancing when the audio server or the
   worker stalls.
3. Every buffer after the check, through sink, monitor and browser, is analysed;
   a check in front of an unexamined buffer proves nothing.
4. It is a product decision of its own, with its engineering cost and its value
   to customers stated first.

## Not carried forward

- Spike protocol revision 5. None is written.
- The K8c clarification (byte bound and time bound never added). It was ruled
  for a protocol that is now superseded; it applies again only if a protocol is
  ever written for a different design.
- The assumptions of revision 4 shown wrong are listed in the report and are not
  corrected in the protocol.
