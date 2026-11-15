# Architecture Decision Closure — ADR-2 physical writer

Status: **ADR-2 DECISION CLOSED** (co-founder ruling on `afcf0a87`, 2026-11-15) · **EVIDENCE CUSTODY PENDING** · #205 unchanged · PublicationContext OFF · Phase 5B NO-GO.

The design decision is final. The administrative closure is not complete until
the raw evidence is in the storage ruled below and its reference is recorded
here.

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
| Production activation of the publication pipeline under #205 | **Blocked**, in full (see "Scope of V6") |
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

## Evidence custody — PENDING

Ruling: the raw archive goes to private object storage the project already
runs, RustFS, in a dedicated bucket `fakeyou-evidence`. No new cloud dependency
is introduced; an earlier ruling naming AWS S3 is withdrawn. Recordings are not
committed to the repository, synthetic PCM included; the house rule stands.

| Item | State |
| --- | --- |
| Results | Committed: `docs/reports/2026-11-15-writer-spike-preflight.json`, with SHA-256 of each raw file |
| Archive | `writer-spike-raw-eba185d7.tar.xz`, 3,088,788 bytes, SHA-256 `974bc6012f2a5c28ea2d9b264c3821948167b747ed05c15d18603f918faa4513`, re-verified on 2026-11-15 |
| Destination | `s3://fakeyou-evidence/fakeyou/writer-spike/2026-11-15/eba185d7/writer-spike-raw-eba185d7.tar.xz` on RustFS. The `s3://` form is the S3-compatible address, not AWS |
| Current location | **The author's machine only**, `.runtime/writer-spike/`. Not reachable by an auditor. The local copy is kept until an upload is verified |
| Upload | **Not done.** The bucket does not exist, and a capability test on the local instance found that retention is not enforced. See `docs/reports/2026-11-16-rustfs-capability.md` |
| Replay tool | `evals/writer_spike/replay.py`, no audio server needed: `python -m evals.writer_spike.replay <directory>` |

Check of the existing deployment, 2026-11-15, read-only:

| Fact | Finding |
| --- | --- |
| Where RustFS runs | Only as the `rustfs` service of `compose.yaml`, `rustfs/rustfs:1.0.0-alpha.81`, on the author's machine. No RustFS appears in the deployment manifests |
| Reachability | Ports bound to `127.0.0.1` only |
| Data | Docker volume `fakeyou_rustfs_data` on the same disk as the local archive |
| Buckets present | `fakeyou` only |
| `fakeyou-evidence` | **Does not exist** (`head-bucket` returned 404). Not created |
| Versioning, Object Lock | Not verified on this version; that needs a bucket to test against |

Two consequences for custody. An upload to this instance would not make the
archive reachable by anyone but the author, and it would sit on the same disk
as the copy it is meant to safeguard. And Object Lock is normally fixed when a
bucket is created, so the bucket should not be created before the retention
configuration is decided.

The capability test of 2026-11-16 adds a third: on this version, a retained
version can be deleted with the ordinary API, in both Governance and
Compliance mode. Object Lock is configured and reported, but it is not
enforced, so nothing held in such a bucket is immutable whatever its metadata
says. The test bucket was removed and the instance returned to its stopped
state. Until a RustFS deployment an auditor can reach holds the bucket, with
retention verified to refuse a deletion there, custody stays pending.

Storage requirements, to be confirmed at upload:

- private bucket with Block Public Access, encryption at rest and versioning;
  Object Lock for the retention period where available;
- read access for authorized auditors and reviewers only; no public or
  temporary link, no shared drive, no PR attachment;
- retention of at least 12 months, longer if organization policy requires;
- SHA-256 of the stored object equal to the value above.

To be recorded here once uploaded: bucket and object key, object version ID,
SHA-256, size, upload date, and the person responsible for the evidence.

Content check of the archive, done on 2026-11-15: 17 entries. Twelve observer
recordings of the container's own null-sink monitor; the relay journal, whose
rows hold only a timestamp, a direction, a byte count and a descriptor count;
the run log; the results file; and the public PulseAudio 17.0 source tarball.
A pattern search of the three text files for keys, tokens, passwords, private
keys, e-mail addresses and local user paths found nothing. The container had no
audio input from a meeting or a host device. This is the author's check, not an
audit.

### What the raw files do and do not support

The diagnostic results record 50 of 50 confirmed late outputs in each of the two
daemon-stall cells. The raw recordings support the presence of the clips and
the gaps in observer delivery. They do not let an auditor reconstruct the
lateness of each clip.

- Recomputed from the recordings by the author: clips found whole per cell are
  50, 50, 50, 50, 0, 50, matching the report; each daemon-stop recording holds
  exactly 50 delivery gaps longer than 250 ms and the other four hold none.
- Not recoverable: the instant of each write call and of each resume. The
  harness held them in memory and wrote aggregates only.

This is not an independently reproduced per-clip lateness and must not be
described as one. The limit is accepted for the decision to **reject** the
designs; no rerun is made. The same evidence may not be used later as complete
proof to approve any alternative. Evidence for activation is held to a higher
standard.

## Scope of V6

Ruled on the text of #205: "A writer that can satisfy neither branch of V2
blocks production activation." The contract gives no exception for statements
that carry no V1 freshness bound, and this closure creates none.

| Capability | Ruling |
| --- | --- |
| Current-scope visual speech | Blocked |
| Historical speech through the same publication pipeline | Blocked |
| Spoken abstention through the same pipeline | Blocked |
| Journaled silence | Allowed |
| Observation and transcript | Not blocked by V6 |
| Decision Memory and Decision Intelligence | Not blocked by V6 |
| Phase 5B publication activation | NO-GO |

What is blocked is activation of the publication and audio subsystem within the
scope of #205, not the product as a whole. Historical speech or spoken
abstention by any other route would need an explicit contract amendment with
its own safety case and acceptance controls.

## What stays in force

- ADR-1 and ADR-3: direction accepted, nothing implemented. The NC-76 / I7
  amendment still needs its own contract review. Closing ADR-2 approves neither
  of them, nor the implementation stack #206–#217.
- No custom audio engine project is opened by this closure.

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
