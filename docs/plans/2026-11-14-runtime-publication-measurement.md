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
| Sentence to evidence binding | The perception layer, at the point where the evidence identity is known and before the sentence enters playback | Attach the gather attempt, the snapshot identity, the observation time, the required visual kinds, and the claim scope to the reply |
| Pre-publication gate | The audio layer, in `PlaybackEngine`, immediately before the first PCM write of a clip | Revalidate the bound evidence and stop the clip when the current-state claim is provably invalid |
| Local playback telemetry | The audio layer | The existing `playback_started`, `playback_audio_started`, `playback_finished`, and `playback_dropped` events, including the drop reason |
| Participant audio observation | An independent, consent-aware adapter | Report the audible start and end of a sentence from the participant's side, with its own clock and uncertainty |
| Measurement and reporting | The reporting layer | Join the binding, the local telemetry, and the participant observation into a measurable and compliant verdict |

The production code never imports a module from `evals`. The contract that both sides
share lives in production, and the evaluation harness consumes it from there.

## The contracts

### The binding

The binding exists once the evidence identity is known and before the sentence enters
playback. It carries the join identity and the claim:

| Field | Meaning |
| --- | --- |
| `transcript_id`, `sentence_index`, `sentence_sha256` | The sentence identity, matching the production journal |
| `gather_attempt_id` | The evidence attempt the reply was produced from |
| `snapshot` | The full snapshot identity: source, subject, stream, epoch, sequence, generation |
| `observed_at` | When the frame was observed, on the common monotonic timeline |
| `required_kinds` | The visual kinds the claim depends on |
| `scope` | `current`, `historical`, or `no_visual` |
| `provider_attempt_id` | The provider attempt that produced the evidence |

The binding granularity available today is the turn reply, not the individual sentence.
A reply produced from one evidence attempt binds every sentence of that reply to it. The
plan does not claim more than the runtime can prove, and a sentence that relied on no
evidence inside a reply that had evidence is still bound to that evidence, which is the
conservative direction.

### The gate

The gate answers one question before the first PCM write: is the bound current-state
claim still valid? It returns one of three outcomes.

| Outcome | Meaning | What the runtime does |
| --- | --- | --- |
| `publish` | The claim holds | The clip is written as usual |
| `prevent` | The claim is provably invalid, for example the frame is older than the bound or the scene changed | The clip is dropped before any PCM is written and the drop reason is published |
| `unknown` | The gate could not establish the state, for example the observer did not answer in time | The publication is not blocked, and the measurement is not measurable |

`unknown` does not silence the twin. The gate exists to stop audio that is provably
stale, not to let an observer hiccup decide whether Fakeyou speaks. The unknown outcome
is recorded as an instrumentation gap in the measurement, never as compliance.

### The participant observation

The observer is an independent adapter with its own clock:

| Field | Meaning |
| --- | --- |
| `publication_id` | The explicit publication identity the observation belongs to |
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
| No deadlock | The gate is bounded by a timeout, and a timeout yields `unknown` |
| A prevented clip is silent | A prevented clip contributes no PCM of its own; the engine's existing fade-out of previously written audio is unchanged and is not part of the prevented clip |
| Cancellation is honoured | A turn cancelled while the gate awaits drops the clip and never writes it |
| Barge-in wins | A clip superseded by a newer turn is dropped even when the gate would have allowed it |

The gate runs once per clip, never per audio frame. A clip that waited in the queue
while the meeting moved on is exactly the case the gate exists for, so the gate runs at
the write boundary rather than at the synthesis boundary.

## Failure and cancellation semantics

| Situation | Gate | Audio | Measurement |
| --- | --- | --- | --- |
| The claim is provably stale at the write boundary | `prevent` | No PCM is written | Publication prevented |
| The scene changed after the evidence was gathered | `prevent` | No PCM is written | Publication prevented |
| The observer does not answer in time | `unknown` | The clip plays | Not measurable |
| The participant did not consent to observation | not applicable | The clip plays | Not measurable |
| The clocks cannot be aligned | `unknown` | The clip plays | Not measurable |
| The clock uncertainty exceeds 50 ms | `unknown` | The clip plays | Not measurable |
| The turn is cancelled during the gate | not applicable | No PCM is written | Not measurable |
| A newer turn supersedes the clip | `prevent` or `unknown` | No PCM is written | Not measurable |
| The provider call for the evidence failed | not applicable | The clip plays without the claim | Not measurable |

## The three results the plan must keep apart

| Result | Meaning |
| --- | --- |
| Publication prevented | The runtime proved the claim invalid and did not speak |
| Publication observed but noncompliant | The audio was written and observed, and the product broke the freshness or continuity contract |
| Publication not measurable | There was no sufficient evidence to judge either way |

The offline primitives from #196 already produce the last two. The runtime wiring adds
the first one, and it adds it as a distinct outcome rather than as a compliance failure,
because preventing a publication is the runtime behaving correctly.

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
   running.
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

Each gap is an explicit not-measurable condition, never a silent pass.

## Execution and acceptance

1. Add the binding, gate, and observer contracts in production, with their failing
   deterministic tests first.
2. Implement the gate at the write boundary with the four safety properties.
3. Implement the reporting join that produces the three results.
4. Run the offline suite, Ruff, mypy, import contracts, and dependency checks.
5. Open a separate implementation PR for review. No live meeting, no provider
   configuration change, and no paid execution.

Acceptance for the implementation:

1. The binding is formed when the evidence identity is known, before the sentence enters
   playback, and it survives to the write boundary unchanged.
2. The gate runs before the first PCM write, is bounded, and cannot deadlock, leak
   cancelled audio, or let a superseded clip play.
3. A prevented publication, an observed noncompliant publication, and an unmeasurable
   publication are three distinct results.
4. `compliant` never appears for a failed, unknown, or missing observation.
5. Local playback telemetry is never reported as participant hearing.
6. The production package does not import from `evals`.
7. Authorization stays zero paid requests and US$0.

Phase 5B, the live confirmatory benchmark, stays separate and unauthorised.
