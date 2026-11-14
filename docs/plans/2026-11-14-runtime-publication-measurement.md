# Phase 5A Runtime Wiring — measurement inside the publication lifecycle

Plan-only milestone. It follows the approved measurement contract in
`2026-11-14-live-e2e-measurement.md`, merged by #195 as `6bb8cb5`, and the offline
measurement primitives merged by #196 as `84ffc80`.

Authorization stays zero paid requests and US$0. This document proposes the design; it
implements nothing. No live meeting, no provider configuration change, and no production
runtime change belongs in this milestone.

## The distinction this milestone must preserve

Fakeyou can observe when it sends audio to its output device. It cannot claim that is
when a participant heard the sentence. Those are two boundaries, and the measurement
contract keeps them apart.

| Boundary | What it proves | What it is not |
| --- | --- | --- |
| Local playback | The PCM of this sentence was written to the output writer | That a participant heard it |
| Participant observation | A sentence became audible to the participant, with its own clock | That the local write happened at the same instant |

`playback_audio_started` stays the local write proof. It is published after the first
PCM reaches the writer, so it is a valid telemetry event and a valid join key, and it is
too late to be a gate that prevents stale audio from being sent.

## The production lifecycle as it is today

| Step | Location | What exists |
| --- | --- | --- |
| Sentence preparation | `conversation/turn_graph.py` `TurnSink.prepare` | The sentence is scrubbed, an index is assigned, and the speaker prepares it with `sentence_index` and `transcript_id` |
| Speech submission | `conversation/turn_graph.py` `TurnSink.say` | The live check runs for streamed replies, the barge is rearmed, and `speaker.speak_prepared` is called |
| Playback queue | `audio/playback.py` `PlaybackEngine._run` | The clip is dequeued, stale and closed clips are dropped, `playback_started` is emitted, and a play task consumes the audio |
| PCM write | `audio/playback.py` `PlaybackEngine._write_frame` | The thaw gate is awaited, the frame is written through `VoiceWriter.write`, and `playback_audio_started` is emitted after the first write |
| Output device | `audio/pulse_writer.py` | The PCM reaches the audio device through a bounded executor |
| Production journal | `orchestration/spoken_log.py` | The playback events carry `transcript_id`, `sentence_index`, and the normalised sentence hash |

The participant side has no observer. A sentence has no proven audible start or end from
the participant's device, so any live compliance claim is not measurable today.

## Ownership of every seam

| Seam | Owner | Responsibility |
| --- | --- | --- |
| Evidence provenance | The perception layer, when the evidence identity is known | Preserve gather attempt, visual kind, full snapshot, provider attempt, capture provenance and observation timestamps; this is not a complete sentence binding |
| Publication binding | Turn/speech orchestration, after `TurnSink.prepare` assigns the index and the final scrubbed playback text is known | Combine immutable evidence provenance with the complete sentence and publication identity before submitting the clip to playback |
| Pre-publication gate | The audio layer, in `PlaybackEngine`, immediately before the first PCM write of a clip | Revalidate the supplied binding; prevent an invalid or unresolved current-state visual claim from being written |
| Local playback telemetry | The audio layer | The existing playback event kinds and drop reason, carrying the same immutable publication identity as the gate and binding |
| Participant audio observation | An independent, consent-aware adapter | Report the audible start and end of a sentence from the participant's side, with its own clock and uncertainty |
| Measurement and reporting | The reporting layer | Join the binding, the local telemetry, and the participant observation into a measurable and compliant verdict |

The production code never imports a module from `evals`. The contract that both sides
share lives in production, and the evaluation harness consumes it from there.

## The contracts

### The binding

Binding is formed in two stages, following the actual production lifecycle. Perception
produces evidence provenance without guessing a sentence index or sentence hash.
Turn/speech orchestration forms the complete publication binding only after the final
scrubbed text and sentence index are known, before playback submission. `SpokenLog.observe`
currently computes the hash of text handed to playback; the orchestration binding must
use the same normalization and digest, not the original model text.

The immutable publication binding carries:

| Field | Meaning |
| --- | --- |
| `publication_id` | A fresh explicit identity for this publication attempt; repeated playback gets a new ID even when the text and sentence identity are unchanged |
| `meeting_id`, `turn_id`, `request_id`, `transcript_id` | The complete meeting, intended turn, reply request, and transcript identity preserved from the #196 binding contract |
| `sentence_index`, `sentence_sha256` | Index assigned during preparation and hash of the final scrubbed playback text; the digest is SHA-256 of `" ".join(text.split()).encode()`, matching `SpokenLog` and #196's `sentence_hash` |
| `gather_attempt_id` | The evidence attempt the reply was produced from |
| `snapshot` | The full snapshot identity: source, subject, stream, epoch, sequence, generation |
| `captured_at`, `capture_clock_domain`, `capture_time_origin` | Original frame capture/acquisition timestamp, its clock domain, and whether it describes source capture, local acquisition, or ingress; retain the declared origin without upgrading its meaning |
| `capture_monotonic`, `capture_mapping_uncertainty_seconds` | Capture time mapped to the local monotonic domain, with measured uncertainty; absent if the mapping is unavailable |
| `observed_at`, `observation_clock_domain` | Separate observation timestamp and clock domain, with the producer's meaning recorded; never a replacement for capture time or an assumed inference-end timestamp |
| `required_kinds` | The visual kinds the claim depends on |
| `scope` | `current`, `historical`, or `no_visual` |
| `provider_attempt_id` | The provider attempt that produced the evidence |

The gate result separately records `validation_checked_at` and `validation_clock_domain`,
linked by the complete publication identity. These are created when revalidation runs;
they are neither capture/observation time nor a mutation of the immutable input binding.

The evidence-use granularity available today is the turn reply, not an independently
proven per-sentence choice of evidence. Orchestration attaches the reply's provenance to
each prepared sentence; this conservative association does not assert semantic support.
Each publication attempt still has its own complete identity. Playback receives that
binding unchanged and carries it through the gate, queue, local telemetry, and observer
join. It neither reconstructs it from the latest gather event nor hashes an earlier,
unscrubbed sentence. The tuple `transcript_id + sentence_index + hash` is not a unique
publication identity. A replayed clip has a new `publication_id`, with its original
sentence/evidence references retained explicitly; observations cannot cross-join attempts.

Capture and observation time remain distinct in the shared production contract and its
evaluation consumers. At the local gate, capture age uses local write-boundary time minus
mapped capture time. Participant frame age uses independently observed audible start
minus mapped frame capture time, including mapping uncertainty as specified by #195.
Neither calculation uses `observed_at` as a fallback. For example, capture at 90.0 s,
observation at 99.0 s, and local write at 100.0 s means a capture age of 10.0 s, not 1.0 s.

If capture time cannot be mapped, retain the original timestamp/domain/origin and report
capture age as unmeasurable. The local gate may record a correctly named observation age
or liveness check from available observations, but those cannot prove the capture-age
bound. For a current-state claim, an unresolved required capture-age check remains
unknown visual validity and requires safe resolution before publication. The #196
primitive's existing `observed_at` field is not an approved shortcut to capture time;
the separate implementation PR must make timestamp semantics explicit in the shared
contract and evaluation consumers. This plan changes no existing code or archived data.

### The gate

The gate answers one safety question before the first PCM write: is the bound current-state
visual claim valid at this local boundary? Participant audio measurement availability is
a separate component; a missing observer cannot decide whether otherwise safe speech plays.

| Visual validity | Meaning | What the runtime does |
| --- | --- | --- |
| `valid` | Required local visual validity checks are established | Allow the clip; participant compliance still needs an independent audible observation |
| `invalid` | Evidence proves a violated bound or an incompatible subject/scene | Prevent the clip before any of its PCM is written; retain the reason |
| `unknown` | A required current-state visual check cannot be established, including revalidator timeout or missing capture-time mapping | Do not publish the unqualified current-state claim; return a prevented/unresolved outcome for upstream refresh, historical framing, or abstention |

Participant observation separately has an available or unavailable/unknown measurement
state. Observer timeout, lack of consent, or missing participant clock mapping does not
block speech when local visual validity is established, or the clip has an independently
appropriate `historical`/`no_visual` scope. It produces a not-measurable participant result,
never a compliant result. No participant observer is awaited as a prerequisite for speech.

Visual revalidator timeout is different: bounded execution must prevent the original
current-state clip, not turn uncertainty into permission to publish it. Orchestration can
refresh evidence, prepare explicitly historical wording, or prepare an abstention within
the existing authorization/budget. Unless that resolution completes, none of the unsafe
clip's PCM is written. Playback does not rewrite the sentence or silently change its
scope to historical. Revised wording is prepared and hashed upstream with a new publication
identity. Fake refresh/fallback tests require no real provider call.

A valid local gate alone does not prove the 250 ms bound relative to participant hearing.
An unavailable participant observation leaves that claim unmeasured; an observed remote
delay or later scene change can still make the published outcome noncompliant.

### The participant observation

The observer is an independent adapter with its own clock:

| Field | Meaning |
| --- | --- |
| `publication_id` | The explicit publication identity the observation belongs to |
| `meeting_id`, `turn_id`, `request_id` | The full publication context, joined through the explicit ID and compared where carried by the observer |
| `transcript_id`, `sentence_index`, `sentence_sha256` | The sentence identity, compared against the binding |
| `started_at`, `ended_at` | The audible interval on the participant's timeline |
| `clock_domain` | Which clock the timestamps came from |
| `uncertainty_seconds` | The uncertainty of the clock mapping |

An observer that has no consent, or that cannot see the sentence, returns no
observation. No observation means not measurable.

## The asynchronous seam, and what makes it safe

The seam is inside the playback play task, immediately before the first
`VoiceWriter.write` of a clip and before `playback_audio_started` is emitted.

The write boundary is the only correct place for the freshness check. A gate that ran
when the clip was queued would judge the claim against an earlier moment, and the
evidence can go stale while the clip waits behind other audio. At the write boundary the
age is measured against the moment the audio would actually leave the device.

The cost is latency. The audio stream is pulled sequentially, so the gate's duration is
added to the time to first audio, and the gate must therefore be fast and bounded. The
first frame is already buffered when the gate runs, so the synthesis has produced its
first chunk, but the pull cannot continue while the gate awaits.

The seam must satisfy four properties, each with a deterministic test.

| Property | Requirement |
| --- | --- |
| No deadlock | Visual revalidation is bounded by a timeout; timeout yields unknown validity and prevents the unresolved current-state clip while leaving the worker usable |
| A prevented clip is silent | A prevented clip contributes no PCM of its own; the engine's existing fade-out of previously written audio is unchanged and is not part of the prevented clip |
| Cancellation is honoured | A turn cancelled while the gate awaits drops the clip and never writes it |
| Barge-in wins | A clip superseded by a newer turn is dropped even when the gate would have allowed it |

The gate runs once per clip, never per audio frame. A clip that waited in the queue
while the meeting moved on is exactly the case the gate exists for, so the gate runs at
the write boundary rather than at the synthesis boundary.

## Failure and cancellation semantics

| Situation | Visual validity / independent veto | Audio | Measurement |
| --- | --- | --- | --- |
| The claim is provably stale at the write boundary | `invalid` | No PCM is written | Publication prevented |
| The scene changed after the evidence was gathered | `invalid` | No PCM is written | Publication prevented |
| Participant audio observer unavailable or timed out; local visual validity established | `valid` | The clip plays | Participant publication not measurable |
| The participant did not consent to observation; local visual validity established | `valid` | The clip plays | Participant publication not measurable |
| Participant audio clock cannot be aligned, or mapping uncertainty exceeds 50 ms; local visual validity established | `valid` | The clip plays | Participant publication not measurable |
| Current-state visual revalidator timed out or cannot establish validity | `unknown` | Original current-state clip writes no PCM; upstream must refresh, explicitly reframe, or abstain | Prevented with unknown validity; not evidence of a proven stale claim |
| Capture time cannot be mapped for a required current-state age check | `unknown` | Original current-state clip writes no PCM pending safe resolution | Capture-age limit not measurable; observation age cannot satisfy it |
| The turn is cancelled during the gate | Cancellation veto, regardless of visual validity | No PCM is written | Not measurable |
| A newer turn supersedes the clip | Supersession veto, regardless of visual validity | No PCM is written | Not measurable |
| The provider call for the evidence failed | No current visual claim is authorized by the failed call | A separately prepared safe failure/abstention response may play; the failed claim is not leaked | Availability outcome retained; no semantic-success or compliance inference |

## The three results the plan must keep apart

| Result | Meaning |
| --- | --- |
| Publication prevented | The original clip contributed no PCM because validity was invalid or unresolved; preserve which reason applied |
| Publication observed but noncompliant | The audio was written and observed, and the product broke the freshness or continuity contract |
| Publication not measurable | There was no sufficient evidence to judge either way |

The offline primitives from #196 already produce the last two. The runtime wiring adds
the first one, and it adds it as a distinct outcome rather than as a compliance failure,
because preventing an invalid or unresolved current-state publication is safe runtime
behavior. An unknown-validity prevention is not relabelled as a proven product violation.

## Deterministic test plan

The implementation PR carries these tests, all offline, with fake providers, fake
observers, and a deterministic clock.

1. The gate prevents a clip whose bound frame is older than the bound at the write
   boundary, and no PCM reaches the writer.
2. The gate allows a clip whose bound frame is fresh, and the PCM is written.
3. A clip that was queued while the meeting moved on is prevented at the write boundary.
4. A turn cancelled while the gate awaits writes no PCM and leaves no task behind.
5. A barge-in during the gate drops the clip and writes nothing.
6. A gate that never answers is bounded by its timeout and leaves the playback worker
   running, while preventing any PCM from its unresolved current-state clip.
7. A prevented clip contributes no PCM of its own, and the engine's existing fade-out of
   previously written audio is unchanged.
8. The local telemetry reports the local write and the drop reason, and never claims a
   participant observation.
9. A participant observation with a mismatched sentence identity does not join.
10. A participant observation with an uncertainty above 50 ms is not measurable.
11. A missing participant observation is not measurable.
12. The binding survives the turn, the queue, and the playback as one explicit identity.
13. The production package does not import anything from `evals`, proven by an import
    contract.
14. The three results are reported as three distinct values, and the compliant verdict
    never appears for a failed or unknown observation.
15. With independently valid local visual evidence but no participant audio observer,
    the clip plays and participant measurement is unknown/not measurable. Compare this
    with a timed-out visual revalidator for the same current-state claim: no PCM from
    that clip is written, and a safe upstream resolution is required.
16. Perception supplies evidence provenance before sentence preparation. Orchestration
    binds the index and hash only after scrubbing, using the exact normalized playback
    text. Verify that the binding hash and `SpokenLog` hash agree when scrubbing changes
    the model's original sentence, with every #196 identity field preserved.
17. Two publication attempts have identical meeting, turn, transcript, request, sentence
    index/hash, and text but distinct `publication_id` values. Each gate decision, PCM event, and
    participant observation joins only its own attempt; one observation cannot satisfy both.
18. Negative control: capture at 90.0 s, observation at 99.0 s, local write boundary at
    100.0 s. The current-state clip is prevented for a 10.0-second capture age despite a
    1.0-second observation age. Assert the two timestamps and origins remain distinct.
19. Capture time has no mapping to the local monotonic domain but observation time is
    recent. Capture-age compliance is not measurable; the current-state validity check
    stays unknown and the unqualified clip writes no PCM. The diagnostic observation age
    is explicitly named and never substituted for capture age.

## Integration gaps that remain after this milestone

1. No participant-side audio observation exists, so the audible end of a sentence cannot
   be proven in a live session. Every live compliance claim stays not measurable until
   an independent observer exists.
2. The evidence result does not carry the gather attempt id, so the binding needs the
   perception layer to pass it explicitly.
3. The provider attempt id does not exist in production, so the binding field the offline
   contract carries has no production source yet.
4. The clock mapping between the local monotonic timeline and the participant device has
   no measured uncertainty, so the 50 ms bound cannot be established.
5. The consent state of the participant is not modelled, so the observer contract has
   no production input for it yet.
6. The scene continuity check needs the live frame identity at publication on the
   participant's side, which the local camera frame store does not provide.

7. Capture timestamps and interpreter observation timestamps need distinct provenance
   and measured clock mapping; the shared binding must not use observation time as
   capture time when the latter is missing or unmappable.

Each gap is an explicit not-measurable condition, never a silent pass. Participant
measurement gaps alone do not block safe speech; gaps that prevent establishing a
required current-state visual validity check prevent that original clip until resolved.

## Execution and acceptance

1. Add the evidence-provenance, complete publication-binding, visual validity gate, and
   independent observer contracts in production, with their failing deterministic tests
   first. Preserve the production/evaluation dependency boundary.
2. Implement the gate at the write boundary with the four safety properties.
3. Implement the reporting join that produces the three results.
4. Run the offline suite, Ruff, mypy, import contracts, and dependency checks.
5. Open a separate implementation PR for review. No live meeting, no provider
   configuration change, and no paid execution.

Acceptance for the implementation:

1. Perception owns immutable evidence provenance; turn/speech orchestration completes
   the binding after final sentence preparation. It includes every #196 identity field
   and a fresh publication ID per attempt, and survives to the write boundary unchanged.
2. The gate runs before the first PCM write, is bounded, and cannot deadlock, leak
   cancelled audio, or let a superseded clip play.
3. A prevented publication, an observed noncompliant publication, and an unmeasurable
   publication are three distinct results.
4. `compliant` never appears for a failed, unknown, or missing observation.
5. Local playback telemetry is never reported as participant hearing.
6. The production package does not import from `evals`.
7. Authorization stays zero paid requests and US$0.
8. Missing participant measurement never blocks otherwise safe speech. Unknown required
   current-state visual validity never permits the unqualified clip to play.
9. Capture timestamp, origin/domain/mapping, observation timestamp/domain, and validation
   time stay separate. Frame age is computed from capture time, with no observation-time
   fallback and no compliance claim when the required mapping is unavailable.

Phase 5B, the live confirmatory benchmark, stays separate and unauthorised.
