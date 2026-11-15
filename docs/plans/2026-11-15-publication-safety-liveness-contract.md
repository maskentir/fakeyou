# Publication Safety & Liveness Contract — Phase 5A Hardening

Base: `main` at `ac75933e`. Design only. This note changes no source, test,
setting, dependency, dataset, cassette, or ledger. `PublicationContext` stays off.
Phase 5B is not authorized. 0 provider requests, US$0.

The readiness sprint (#200 to #204) left the gate correct but unusable: it stops
wrong speech and also stops correct speech. Reading the code for this note also
found a path where wrong speech can pass. This note specifies the behavior that
closes both, as decision tables and negative controls that an implementation must
satisfy. Every row is meant to become a deterministic test.

Revision 3 applies the reviews of revision 1 (`e967419`) and revision 2
(`d8aaa16`). From the first: historical statements are rendered from structured
claims; exact content is separated from source and subject identity; the boundary
before the first PCM write has an explicit commit rule. From the second: visual
speech is render-only, so the model's wording is never published in a visual
turn; a committed decision is distinguished from audio actually written. The
sixteen decisions from both reviews are locked in section 12.

## 1. What is true on `main` today

Read from the code at `ac75933e`. Rows marked *test* are proven by
`tests/test_publication_activation.py`. Rows marked *code* are read from the
source, have no test yet, and have not been reproduced on a real Meet page.

| # | Behavior | Source |
| --- | --- | --- |
| T1 | A claim about a slide shown for more than 5 s is `capture_too_old` | test |
| T2 | A claim written more than 5 s after capture is `capture_too_old` | test |
| T3 | A reply after an insufficient look is `validation_support_unknown` | test |
| T4 | Camera scene identity is stream, epoch, and generation; content is not compared | test |
| T5 | The page emits a screen frame only when its 128×72 thumbnail changes. A static slide produces no later sample at all, so there is nothing to confirm continuity with | code: `assets/perception.js` `sampleScreen` |
| T6 | A screen change of at most 2 changed 8×8 thumbnail blocks stays the same segment (`compare_screens` is `minor`), and the bound snapshot identity is the segment's first keyframe. The gate reports `unchanged` for it | code: `perception/frames.py`, `perception/timeline.py`, `perception/context.py` `_screen_snapshot` |
| T7 | A screen change below the page's emit threshold (2 changed thumbnail blocks) is never sent to Python | code: `sampleScreen` |
| T8 | Every sentence of a turn with visual needs is bound to all gathered evidence with one scope. There is no per-claim link | code: `conversation/turn_graph.py` `_bind_publication` |
| T9 | The page emits a camera frame only when at least 12 thumbnail blocks changed or 120 s passed. In between, liveness is a heartbeat that refreshes `seen_at` on the stored frame without any content | code: `sampleCamera`, `CameraFrameStore.touch` |
| T10 | A sentence in the `historical` scope is `valid` with no check at all, whatever its text says | code: `foundation/publication.py` `evaluate_visual_publication` |
| T11 | The writer protocol has only an asynchronous `write`, and the Pulse writer runs it on an executor thread. There is no synchronous point at which a frame is accepted | code: `audio/playback.py` `VoiceWriter`, `audio/pulse_writer.py` |

T6 and T7 mean a small substantive change, for example one digit, can pass
today's gate as unchanged. This is a potential safety bug that needs a regression
test. The first implementation PR should add the failing test for T6 before
changing anything. T10 is why no model-written text is bound as `historical` (I11), and T11 is why
section 6 asks for an explicit acceptance boundary.

## 2. Invariants

These hold in every table below. A row that would break one is wrong.

| # | Invariant |
| --- | --- |
| I1 | The capture freshness limit stays 5.0 s, the validation age limit 0.25 s, and the accepted clock uncertainty 0.05 s |
| I2 | The capture time, snapshot identity, provider attempt, and inference of the analyzed frame are immutable. No later sample overwrites them |
| I3 | A later sample has its own identity, capture time, and mapping uncertainty. It is evidence of content continuity, never of the claim itself, and never of source or subject identity |
| I4 | The gate, the commit evaluation, the renderer, and every fallback make no vision or language-model request. Nothing in this contract adds one, including re-reading a frame or re-asking the reply model |
| I5 | When support for a claim cannot be proven the decision is `unknown`, never `valid` |
| I6 | `invalid` outranks `unknown`, which outranks `valid`, across the claims of a sentence and the evidence of a claim |
| I7 | A clip's decision is final at its commit (section 6): the final evaluation and the handoff of the first frame, with no suspension between them. A clip whose committed decision is not `valid` hands nothing to the writer. A committed decision authorizes the attempt; it is not evidence that audio was written. After the commit the gate never retracts or rewrites the decision |
| I8 | A prevented sentence is never counted as spoken and never enters the meeting record as twin speech |
| I9 | Nothing here is on by default. Activation needs its own review |
| I10 | The scope of a sentence is assigned by the runtime from deterministic rules. A scope, class, or support declared by a model is input to be checked, never the result |
| I11 | In a turn with visual needs, model-written sentence text is never synthesized or published. Everything spoken there is a runtime-rendered pattern or a catalog entry. Model text is published only in a turn with no visual needs |
| I12 | Local audio is proven only by the playback receipt. A decision, a commit, or a call to the writer is never recorded as spoken |

## 3. Vocabulary

**Analyzed capture (A).** The frame a provider look inspected. It carries the
snapshot reference, the mapped capture instant with its uncertainty, the provider
attempt, the look status (`produced`, `insufficient`, `unavailable`), and the
content digest of the frame.

**Confirmation sample (C).** A later local sample of the same source, taken with
no provider request. It carries its own identity, its own mapped capture instant
and uncertainty, and a content digest (section 4).

**Match level** between A and C:

| Level | Meaning | May extend a claim |
| --- | --- | --- |
| `exact` | Level 1. Digest records are comparable and the digests are equal | yes, for content only |
| `similar` | Level 2. Not exact, but below the existing visual change threshold. The 128×72 blurred thumbnail signature is a `similar` test because it is lossy | no, for any claim class |
| `distinct` | Above the visual change threshold | no |
| `unknown` | A digest is missing, skipped, or the records are not comparable | no |

**Claim.** One assertion, structured as source kind, entity, attribute, value,
polarity, class, dependence, and the evidence item it rests on.

**Claim class.** `specific` is a number, amount, date, time, name, status, quoted
text, table cell, or chart value. `general` is everything else. A claim that is
not classified is `specific`. The class is recorded for audit; no class may use
Level 2.

**Claim dependence.** What must still hold for the claim to be true now:

| Dependence | Meaning | What an exact match proves |
| --- | --- | --- |
| `content` | True of the visible content, whoever shows it ("the slide lists three risks") | the claim's content basis |
| `source` | True of a particular share, stream, or presenter ("the slide Ayu is presenting shows …") | nothing about the source |
| `subject` | True of a particular participant (every camera claim) | nothing about the subject |

A claim with no declared dependence is `source` for a screen and `subject` for a
camera.

**Times.** `boundary` is the commit point of the clip. `a` is `boundary` minus
A's capture instant, `c` is `boundary` minus C's capture instant, `u` is the
mapping uncertainty of the sample in question. "Fresh" means age plus `u` is at
most 5.0 s. Age minus `u` above 5.0 s is "old". Between the two is "uncertain".

## 4. Confirmation samples and digests

A static slide produces no sample today (T5). The contract needs every sample to
report, whether or not the picture changed. This changes `perception.js`, which
is not authorized by this note.

**Digest record.** Computed in the page from the decoded pixels of the frame at
its native resolution, with no resize and no lossy transform. Each record carries:

| Field | Purpose |
| --- | --- |
| algorithm and version | comparability |
| width, height, pixel format | comparability; a resolution or format change is not comparable |
| source identity: role, stream, subject | which source the sample is from |
| sequence | per-source, strictly increasing, assigned when the frame is taken |
| media timestamp of the frame | proves the frame is a new one |
| capture time on the page clock, taken when the frame is taken from the track | mapped once to the local clock with its uncertainty |
| digest, or a skip reason | content |

The analyzed capture carries the same record, computed from the same pixels that
were encoded for the provider.

**Sampling rules.**

| # | Situation | Rule |
| --- | --- | --- |
| D1 | Capture time | It is when the frame was taken from the track. The time hashing finished is never used as a capture time |
| D2 | Media timestamp did not advance since the previous sample (stalled decode, repeated frame) | The record is marked `repeated`. It is not a new capture and does not refresh freshness, however recently it was hashed |
| D3 | Sequence lower than or equal to one already ingested | Discarded for confirmation and journaled |
| D4 | Capture time earlier than the previous sample's, with a higher sequence | Match level `unknown` until a later consistent sample |
| D5 | Gap in sequence | Journaled. Freshness comes only from the latest ingested sample. What happened in the gap is unknown and is not assumed unchanged for `source` or `subject` claims |
| D6 | Hashing skipped or over its CPU budget | The record carries the skip reason and no digest; match level `unknown`. A previous digest is never reused |
| D7 | Width, height, pixel format, algorithm, or version differ between A and C | Not comparable; match level `unknown` |
| D8 | Heartbeat with no record | Liveness only; match level `unknown` |
| D9 | No sample within one freshness window | The latest sample ages out; the rows for an old confirmation apply |

"Latest sample" always means the highest ingested sequence of that source.

The CPU budget per sample is not set here. It needs a measurement on a real page,
which is not authorized. The contract only fixes what happens when it is exceeded
(D6).

## 5. Contract 1 — Static slide (screen content continuity)

A `content` claim may be published from an analyzed capture older than 5 s only
when the latest sample is fresh and matches it exactly. The analyzed capture
keeps its own time; the decision records both.

This table is for `content` claims. Section 8 adds the identity conditions for
`source` and `subject` claims.

| # | A status | A age | Latest sample after A | Match | C age | Decision | Reason | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S1 | produced | fresh | none | n/a | n/a | valid | `ok` | none |
| S2 | produced | fresh | C | exact | fresh | valid | `ok` | none |
| S3 | produced | old | C | exact | fresh | valid | `continuity_confirmed` | none |
| S4 | produced | old | none | n/a | n/a | invalid | `capture_too_old` | historical |
| S5 | produced | old | C | exact | old | invalid | `confirmation_too_old` | historical |
| S6 | produced | old | C | exact | uncertain | unknown | `confirmation_age_threshold_uncertain` | historical |
| S7 | produced | any | C | similar | any | unknown | `content_changed_unverified` | historical |
| S8 | produced | any | C | distinct | any | invalid | `scene_changed` | historical |
| S9 | produced | any | C | unknown (D6, D7, D8) | any | unknown | `continuity_unknown` | historical |
| S10 | produced | any | share stopped | n/a | n/a | invalid | `scene_changed` | historical |
| S11 | produced | uncertain | none | n/a | n/a | unknown | `capture_age_threshold_uncertain` | historical |
| S12 | produced | unmapped at capture | any | any | any | unknown | `capture_time_unmapped` | abstention |
| S13 | produced | any | C unmapped, `u` above 0.05 s, or another clock domain | exact | n/a | unknown | `confirmation_time_unmapped` | historical |
| S14 | produced | any | C captured before A, or after `boundary` | exact | n/a | unknown | `confirmation_time_inconsistent` | historical |
| S15 | produced | old | exact C, then a later similar or distinct sample | n/a | n/a | as S7 or S8 | as S7 or S8 | historical |
| S16 | produced | old | distinct sample, then exact C (content left and came back) | exact | fresh | valid for `content` claims only | `content_confirmed_after_interruption` | none |
| S17 | produced | old | exact C marked `repeated` (D2) | exact | n/a | as if C were absent | per S4, S5 | historical |
| S18 | produced | fresh | A has no digest record | n/a | n/a | valid only while no later sample exists (S1); with any later sample, unknown | `continuity_unknown` | historical |

Notes that are part of the contract:

- An older exact match does not outvote a newer non-exact sample (S15).
- S7 tightens today's behavior (T6): a `similar` sample no longer counts as
  unchanged, whatever the age of A.
- S16 proves the content is on the screen now. It does not prove the same share,
  stream, or presenter. The decision records that the source was interrupted.
- A's capture instant is mapped once, when A is captured, and stored. It is never
  re-derived later, so the 120 s mapping span no longer turns a long-lived slide
  into `capture_time_unmapped`. If it could not be mapped at capture it stays
  unmapped (S12).
- The decision carries A's and C's identities, both capture instants, both
  uncertainties, both digest records, and the match level.

## 6. Contract 2 — Slow reply and the commit boundary

The limit stays 5 s. A reply that arrives late is published only when the latest
sample is fresh and matches the analyzed capture exactly. That is Contract 1
evaluated at the moment of use. What this section adds is when "the moment of
use" is, and what it does and does not prove.

**Three different facts.** They are recorded separately and never inferred from
one another.

| Fact | Meaning | Proven by |
| --- | --- | --- |
| Committed decision | The gate authorized this attempt on the last state it could see | the decision record, with the store revision it read |
| Local audio written | PCM of this attempt reached the local output | the playback receipt (`audio_started`, `interrupted_after_audio`, `completed`) |
| Heard by a participant | Someone heard it | a consented participant observation; not available in production |

A committed `valid` decision is not a statement that anything was spoken.
Cancellation, a write failure, or a zero-byte outcome after the commit leaves the
decision as it was and the receipt as `dropped_before_audio` or a failure. Such an
attempt is not counted as spoken and does not enter the meeting record (I8).

**Linearization.** Samples reach the gate in one order: the order in which the
runtime ingests them into the perception store. That order, not capture time, is
what the gate can see.

1. **Preliminary decision.** The asynchronous gate reads the store and returns a
   decision together with, per bound source, the sequence of the latest sample it
   read. It has the existing 0.25 s timeout.
2. **Waits.** The engine may still wait after that: thaw, queue, and anything the
   writer needs before it can take a frame.
3. **Commit.** One section with no suspension point, containing in order: the
   final synchronous evaluation of the binding against the store as it is then,
   and the handoff of the first PCM frame to the writer. Digest comparison is
   local and constant-time, so the evaluation needs no await and no provider.
4. **After the commit.** The write completes, fails, or is cancelled, and the
   receipt says which. A sample ingested after the commit does not change the
   committed decision and does not make the gate stop the clip. It is recorded as
   post-commit evidence against the publication ID, with its position relative
   to the commit and to the completion of the first write, and it is evaluated
   by post-publication continuity.

**Atomic acceptance boundary.** `await writer.write()` is not assumed to make the
commit and the write one operation. On `main` the writer protocol has only an
asynchronous `write`, and the Pulse writer runs it on an executor thread, so
there is no synchronous acceptance point today. The implementation must provide
one of these and state which:

| # | Design | Rule |
| --- | --- | --- |
| W1 | The writer exposes a non-suspending accept or enqueue | The final evaluation and the accept are in one non-suspending section |
| W2 | The writer can only be awaited | Every asynchronous wait the writer needs (readiness, back-pressure) happens first; the final evaluation runs after that wait and immediately before a handoff that does not suspend before the frame is taken |

In either design no asynchronous wait lies between the final evaluation and the
handoff. If a wait is unavoidable there, the evaluation is repeated after it. A
design that cannot show this does not satisfy the contract.

A change the sensor has not delivered cannot be seen by the gate. The exposure is
one confirmation interval plus delivery delay. It is reported after the fact,
never repaired by rewriting the decision.

| # | Situation | Committed decision | Reason | Fallback |
| --- | --- | --- | --- | --- |
| R1 | Statement ready within 5 s of A, no later sample | valid | `ok` | none |
| R2 | Ready after 5 s, latest sample exact and fresh | valid | `continuity_confirmed` | none |
| R3 | Ready after 5 s, no fresh sample (sampling stale or stopped) | invalid | `confirmation_too_old` or `capture_too_old` | historical |
| R4 | Ready after 5 s, latest sample similar | unknown | `content_changed_unverified` | historical |
| R5 | Ready after 5 s, latest sample distinct | invalid | `scene_changed` | historical |
| R6 | Non-exact sample ingested while the preliminary gate is running, before it reads the store | per S7 or S8 | per S7 or S8 | historical |
| R7 | Non-exact sample ingested after the preliminary gate read the store, before the commit | per S7 or S8; no handoff | per S7 or S8 | historical |
| R8 | Exact sample ingested between the preliminary decision and the commit | valid, with the newer sample recorded | `continuity_confirmed` | none |
| R9 | Preliminary decision valid, commit later, no newer sample | re-evaluated at the commit; if the latest sample is no longer fresh, per S5 or S6 | per S5 or S6 | historical |
| R10 | Latest sample fresh at the preliminary decision, old at the commit | invalid | `confirmation_too_old` | historical |
| R11 | Non-exact sample ingested after the commit, before the first write completed | committed decision stands; whether audio was published is determined by the playback receipt | `ok`; the sample is post-commit evidence | none |
| R12 | Non-exact sample captured before the commit, ingested after it | as R11; the evidence records that the change preceded the commit | `ok` | none |
| R13 | Second statement of the same turn | its own preliminary decision and its own commit | per table | per table |
| R14 | Preliminary gate exceeds its 0.25 s timeout | prevented, no decision | `unresolved`, as today | abstention |
| R15 | Playback frozen after the preliminary decision, thawed later | the commit is after the thaw; evaluated there | per table | per table |
| R16 | Barge-in or stop before the commit | no commit; receipt `dropped_before_audio`, as today | n/a | none |
| R17 | Committed valid, caller cancelled or playback stopped before the first write completed | committed decision stands; receipt `dropped_before_audio`; not spoken | `ok` | none |
| R18 | Committed valid, the first write fails | committed decision stands; receipt reports the failure with zero local audio; not spoken | `ok` | none |
| R19 | Committed valid, the clip yields zero bytes | committed decision stands; receipt `dropped_before_audio`; not spoken | `ok` | none |
| R20 | Writer needs a wait before it can take a frame; a non-exact sample is ingested during that wait | the wait precedes the final evaluation (W2), so the sample is seen: per S7 or S8, no handoff | per S7 or S8 | historical |

## 7. Contract 3 — Abstention

Three kinds of statement, with different scopes:

| Kind | Scope | Definition | Gate |
| --- | --- | --- | --- |
| Epistemic uncertainty | `no_visual` | Says only what the speaker does or does not know. Asserts nothing about what is or was visible | valid, no freshness |
| Historical observation | `historical` | Refers explicitly, in the past, to the frame that was checked | no freshness; section 10 |
| Current-state claim | `current` | Any assertion about what is visible now, positive or negative | Contracts 1, 2, 4, 5 |

Classification is by the runtime (I10):

- An abstention is spoken only when the runtime selects a catalog entry by its
  condition. The gate accepts an abstention binding only when its sentence
  digest equals the digest of a catalog entry of the stated version and
  language. Normalization is whitespace and case only. There is no fuzzy or
  semantic matching.
- Model-written text in a turn with visual needs is never published (section 9),
  so it cannot serve as an abstention either.
- After a look that did not produce, no current statement can be valid. The
  runtime speaks the catalog entry for that condition. The reply model is not
  asked again.
- The identity of the failed look (gather attempt, snapshot, provider attempt,
  status) is journaled with the abstention. An epistemic statement keeps the
  gather attempt ID on its binding and carries no evidence, as the binding
  already requires.

**Abstention catalog v1** (approved wording):

| ID | Condition for use | Scope | English | Indonesian |
| --- | --- | --- | --- | --- |
| AB1 | Not enough reliable information to answer | `no_visual` | "I don't have enough reliable information to answer that." | "Saya belum punya informasi yang cukup andal untuk menjawabnya." |
| AB2 | A frame was actually inspected and the look was `insufficient` | `historical` | "I couldn't determine that from the frame I checked." | "Saya tidak bisa memastikan itu dari gambar yang saya periksa." |
| AB3 | No frame could be inspected: look `unavailable` or failed | `no_visual` | "I couldn't inspect the visual information needed to answer." | "Saya tidak bisa memeriksa informasi visual yang diperlukan untuk menjawabnya." |

AB2 requires a snapshot and a provider attempt on the look. Without both the
runtime uses AB3. The Indonesian text lives in JSON, not in `.py`.

| # | Situation | Look | Result | Reason |
| --- | --- | --- | --- | --- |
| A1 | Runtime selects AB1 | insufficient information | `no_visual`, valid | `ok` |
| A2 | Runtime selects AB2 | insufficient, frame inspected | `historical`, valid | `ok` |
| A3 | Runtime selects AB3 | unavailable | `no_visual`, valid | `ok` |
| A4 | AB2 requested where no frame was inspected | unavailable | runtime selects AB3 | `ok` |
| A5 | Model writes "I cannot see that on the screen right now." | insufficient | model text discarded; AB2 spoken | `claim_undeclared` |
| A6 | Model writes "There is nothing on the screen." | insufficient | model text discarded; AB2 spoken | `claim_undeclared` |
| A7 | Model writes "I'm not sure, but revenue is $1.2M." with a declared claim | insufficient | claim not supported; AB2 spoken | `validation_support_unknown` |
| A8 | Abstention binding whose text is a catalog entry followed by a factual clause | any | prevented | `abstention_not_in_catalog` |
| A9 | Abstention binding whose text differs from an entry only in spacing or case | any | valid | `ok` |
| A10 | Abstention binding whose text differs from an entry by one word | any | prevented | `abstention_not_in_catalog` |
| A11 | Model declares a scope for anything | any | declaration ignored | n/a |
| A12 | AB1 after a `produced` look whose claim could not be supported or rendered | produced | `no_visual`, valid | `ok` (it withholds, it does not claim) |
| A13 | Catalog entry with no publication context (today's default) | any | spoken as today | n/a |
| A14 | AB1 in a turn with no visual needs | n/a | `no_visual`, valid | `ok` |
| A15 | Abstention binding naming a catalog version or language that does not exist | any | prevented | `abstention_not_in_catalog` |

A1 to A4, A9, and A12 to A14 are the "safe speech is not silenced" half. A5 to A8,
A10, A11, and A15 are the "a claim cannot dress as an abstention" half. Both
halves are required of the same implementation.

## 8. Contract 4 — Identity: camera continuity, source, and subject

Stream identity and liveness prove the camera is on and is the same stream. They
do not prove that what was described is still there. Exact content proves what is
visible, not who shows it or who it is. A claim needs both the content condition
and the identity condition its dependence requires.

**Identity condition by dependence.**

| # | Dependence | Identity required at the commit point | When it fails |
| --- | --- | --- | --- |
| N1 | `content` | none beyond the source kind | n/a |
| N2 | `source` (screen) | same share: stream, epoch, generation, and no stop in between; presenter attribution unchanged per the state gate's revision | unknown `source_identity_changed`; invalid `scene_changed` when the share stopped |
| N3 | `subject` (camera) | same participant, stream, epoch, and generation; camera on; live | invalid `scene_changed`; invalid `subject_changed` when the sample is another participant |
| N4 | not declared | treated as `source` for a screen, `subject` for a camera | as N2, N3 |

A claim about who is presenting is a state claim and is decided by the state gate
from #199, not by content match.

**Camera content.** A live camera never repeats a frame exactly, so Level 1 is
rare for cameras. The contract does not weaken Level 1 to make camera claims
pass; it routes them to historical framing. Every camera sample carries a digest
record at the existing 4 s interval instead of only the heartbeat of T9.

| # | A status | A age | Latest sample after A | Match | Decision | Reason | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- |
| C1 | produced | fresh | none | n/a | valid | `ok` | none |
| C2 | produced | fresh or old | C fresh, identity N3 holds | exact (still image, avatar, frozen virtual camera, not `repeated`) | valid | `continuity_confirmed` | none |
| C3 | produced | fresh | C | similar (same person, small motion) | unknown | `content_changed_unverified` | historical |
| C4 | produced | fresh | C | distinct (pose, framing, lighting, another person in frame) | unknown | `content_changed_unverified` | historical |
| C5 | produced | any | camera off, participant left, epoch or generation changed, stream replaced | any, including exact | invalid | `scene_changed` | historical |
| C6 | produced | any | sample carries another subject | any, including exact | invalid | `subject_changed` | abstention |
| C7 | produced | old | none, or only non-exact | n/a | invalid | `capture_too_old` | historical |
| C8 | produced | any | no sample within the liveness timeout | n/a | unknown | `scene_continuity_unknown` | historical |
| C9 | produced | any | heartbeat only, no digest record (D8) | unknown | unknown | `continuity_unknown` | historical |
| C10 | not produced | any | any | any | per Contract 3 | per Contract 3 | catalog entry |

C4 is `unknown`, not `invalid`: motion does not show the claim is false ("Budi
wears grey" survives a pose change), it shows the gate cannot prove it. C5 and C6
are `invalid` because the subject of the claim is no longer the scene, and an
identical picture does not change that.

**Screen identity.**

| # | Situation | `content` claim | `source` claim |
| --- | --- | --- | --- |
| P1 | Same share throughout, latest sample exact and fresh | valid | valid |
| P2 | Share stopped and restarted, identical content, exact and fresh | valid (`content_confirmed_after_interruption`) | unknown (`source_identity_changed`) |
| P3 | Another presenter shares pixel-identical content | valid (`content_confirmed_after_interruption`) | unknown (`source_identity_changed`) |
| P4 | Content left and came back within the same share (S16) | valid | unknown (`source_identity_changed`): continuity of the source through the gap is not proven |
| P5 | Share stopped, nothing shared now | invalid (`scene_changed`) | invalid (`scene_changed`) |

Consequence, stated plainly: once every 4 s camera sample carries a digest, and
with replies that take several seconds, most camera claims will have a newer
non-exact sample at the commit point and will be spoken in historical framing,
not as current claims. This is accepted, and v1 goes one step further: there is
no current pattern for camera claims (section 9), so every camera claim that is
spoken is a historical statement. The table above still decides whether the
claim's content and identity hold, and that decision is recorded.

## 9. Contract 5 — Claim support and render-only visual speech

Today a sentence written by the reply model is bound to the whole gather (T8) and
published as written. A matcher can prove the claims a sentence declares; it
cannot prove the sentence says nothing more. "Revenue is $1.2M and growing fast"
with one supported revenue claim would pass. So in a turn with visual needs the
model's wording is not published at all.

**Pipeline.**

1. **Propose.** In the reply call that already exists, the model proposes
   structured claims. No additional request is made. The proposal is untrusted
   (I10).
2. **Verify.** The runtime checks each claim against the look's answer and the
   evidence (table L).
3. **Render.** The runtime produces the statement from a fixed, versioned
   pattern, filling it only with approved evidence spans (rules G).
4. **Commit.** The rendered current statement is gated by Contracts 1, 2, 4 and
   the identity conditions at its own commit.
5. **Fall back.** A statement that cannot be published as current becomes a
   rendered historical statement or a catalog abstention (section 10).

Everything that reaches synthesis in a visual turn is the output of a pattern or
a catalog entry. The model's sentences in that turn are journaled by digest and
discarded before synthesis. Turns without visual needs are unchanged.

**L. Support of one claim against the look's answer.** Support is textual and
deterministic. It is evidence that the look said this, not proof of meaning.

| # | Condition | Decision | Reason |
| --- | --- | --- | --- |
| L1 | Evidence item exists, look `produced`; entity, attribute, and value each occur in one clause of the look's answer; normalized values are equal; polarity and status agree; the clause relates exactly one value to that attribute | supported | `ok` |
| L2 | Evidence item not in the binding | unknown | `claim_evidence_missing` |
| L3 | Declared against evidence of another kind or subject | unknown | `claim_evidence_mismatch` |
| L4 | Value occurs in the answer but with another entity or attribute ("Costs $1.2M" for a revenue claim) | unknown | `claim_relation_unsupported` |
| L5 | Value does not occur in the answer after normalization | unknown | `claim_untraceable` |
| L6 | Polarity or status differs (answer says "not approved", "pending", "no longer"; claim says "approved") | unknown | `claim_polarity_unsupported` |
| L7 | Clause holds several values or several attributes and the pairing is ambiguous | unknown | `claim_relation_ambiguous` |
| L8 | Claim lacks a field its shape needs | unknown | `claim_incomplete` |
| L9 | Look `insufficient` or `unavailable` | unknown | `validation_support_unknown` |
| L10 | Evidence decision `invalid` | invalid | that reason |
| L11 | Evidence decision `unknown` | unknown | that reason |

Value normalization, the clause splitter, and the negation and status marker
lists are fixed and versioned per language. A specific-token scan (digits,
currency and percent forms, dates, times, quoted strings) is a negative control
only: it can show that text says more than its claims, and can never show
support.

**Claim shapes v1.**

| Shape | Fields |
| --- | --- |
| Entity–attribute–value | entity, attribute, value |
| Entity–status | entity, status |
| Item presence | item |

**Patterns v1.** A pattern states what the frame or screen shows. It never states
an external fact as independently true.

Historical patterns (approved wording):

| Shape | English | Indonesian |
| --- | --- | --- |
| Entity–attribute–value | "The checked frame listed [attribute] for [entity] as [value]." | "Pada gambar yang saya periksa, [attribute] untuk [entity] tercantum sebagai [value]." |
| Entity–status | "The checked frame listed the status of [entity] as [status]." | "Pada gambar yang saya periksa, status [entity] tercantum sebagai [status]." |
| Item presence | "The checked frame showed [item]." | "Gambar yang saya periksa menampilkan [item]." |

Current patterns. The English entity–attribute–value wording is from the review;
the others follow it and are proposed here (section 14):

| Shape | English | Indonesian |
| --- | --- | --- |
| Entity–attribute–value | "The currently displayed screen lists [attribute] for [entity] as [value]." | "Layar yang sedang ditampilkan mencantumkan [attribute] untuk [entity] sebagai [value]." |
| Entity–status | "The currently displayed screen lists the status of [entity] as [status]." | "Layar yang sedang ditampilkan mencantumkan status [entity] sebagai [status]." |
| Item presence | "The currently displayed screen shows [item]." | "Layar yang sedang ditampilkan menampilkan [item]." |

The current patterns are for screen claims. A camera claim has no current
pattern in v1 and is rendered only historically, which matches the consequence
accepted in section 8.

**G. Field rules.** A claim that fails any of them is `claim_render_unsupported`
and is not rendered in any tense.

| # | Rule |
| --- | --- |
| G1 | Each placeholder is filled with one contiguous span of the look's answer, taken from the clause that satisfied L1. Nothing is taken from the model's sentence or from the model's wording of the claim |
| G2 | A field contains no sentence or clause punctuation and no line break |
| G3 | A field is at most a fixed length (section 14 proposes the number) |
| G4 | A field contains no marker from the fixed negation, temporal ("now", "currently", "right now"), or certainty ("definitely", "certainly") lists |
| G5 | A value field matches the grammar of its type: number, currency, percent, date, time, or quoted string. A status field is the status span of the answer |
| G6 | Polarity is positive. A negated, conditional, or hedged claim has no pattern in v1 |
| G7 | The shape is one of the three, and a pattern exists for the language of the turn |
| G8 | The rendered statement records the pattern ID and version, the claim, and the span offsets of every field |

**M. Statements and turns.**

| # | Situation | Result | Reason |
| --- | --- | --- | --- |
| M1 | Statement text is the output of an approved pattern version; every field passes G; the claim is supported (L1); Contracts 1, 2, 4 and the identity conditions hold at the commit | valid, spoken as a current statement | `ok` |
| M2 | A pattern that carries several claims (none in v1) | every claim must be valid; `invalid` outranks `unknown` outranks `valid` | the deciding claim's reason |
| M3 | Model sentence carries a supported, renderable claim and more ("… and growing fast") | the rendered statement is spoken; the model's text is not | `model_extra_assertion_ignored` on the discard |
| M4 | Claim supported but not renderable (G fails) | not rendered; AB1 | `claim_render_unsupported` |
| M5 | Model sentence in a visual turn with no declared claim | discarded | `claim_undeclared` |
| M6 | Declared claim not supported (L2 to L9) | not rendered; section 10 | the L reason |
| M7 | Turn with no visual needs | model text published as `no_visual`, as today | `ok` |
| M8 | Two claims, one valid and one unknown | two statements decided separately; the valid one is spoken, the other follows section 10 | per statement |
| M9 | The same claim declared twice | rendered once | n/a |
| M10 | Model marks a claim or sentence "non-visual" inside a visual turn | ignored | `claim_undeclared` |
| M11 | Model sentence asserts more than its claims: "and growing fast", "definitely", "right now", or an unsupported negation | never reaches synthesis; only the rendered statement of the supported claim can | `model_extra_assertion_ignored`, or `claim_render_unsupported` when the claim cannot be rendered |
| M12 | A visual turn in which nothing was spoken | one abstention by its condition | per section 10 |
| M13 | A gather that is historical from the start (a question about a previous screen) | claims verified by L and rendered with the historical pattern; the `historical` scope is assigned only after the rendered statement exists with provenance that satisfies section 10; otherwise abstention | per section 10 |
| M14 | Neutral model speech in a visual turn ("Sure, let me check.") | discarded; there is no neutral catalog yet | `claim_undeclared` |

Every statement's decision lists its claim, the pattern, and the reason, so a
prevented or replaced statement can be traced.

## 10. Fallback

Prevention never triggers a vision or language-model request (I4). What may be
spoken instead:

| Fallback | When | What is spoken | Binding |
| --- | --- | --- | --- |
| none | committed decision `valid` | the rendered current statement | as bound |
| historical | every condition in H1 to H6 holds | the historical pattern for the claim's shape | new publication ID, scope `historical`, same evidence, link to the prevented publication |
| abstention | historical is not available | AB1, AB2, or AB3 by its condition | new publication ID, the entry's scope, gather attempt kept |
| silence | an abstention was already spoken in this turn | nothing | journaled |

| # | Condition for a historical statement |
| --- | --- |
| H1 | The look `produced`, and the claim is supported by L1 |
| H2 | The claim passes every field rule G1 to G8 for the historical pattern |
| H3 | A historical pattern exists for the claim's shape and language |
| H4 | A's capture instant is mapped, and `a` plus A's uncertainty is at most 30 s, measured from A's own capture, never from a confirmation sample |
| H5 | When it replaces a prevented current statement, the prevention reason is one of: `capture_too_old`, `confirmation_too_old`, `confirmation_age_threshold_uncertain`, `capture_age_threshold_uncertain`, `content_changed_unverified`, `continuity_unknown`, `scene_changed`, `scene_continuity_unknown`, `source_identity_changed`, `confirmation_time_unmapped`, `confirmation_time_inconsistent` |
| H6 | There is no subject attribution problem: never after `subject_changed`, `claim_evidence_mismatch`, or any reason from L2 to L9 |

The `historical` scope is not a bypass. The gate checks H1 to H6 at the
statement's own commit, and a `historical` binding is accepted only for a
statement the runtime rendered or for AB2. Model-written text is never bound as
`historical` (I11).

| # | Situation | Spoken |
| --- | --- | --- |
| F1 | Supported revenue claim, current statement prevented by `capture_too_old`, A is 12 s old | historical pattern |
| F2 | Same, A is 31 s old | AB1 |
| F3 | Same, A is within its uncertainty of 30 s | AB1 |
| F4 | Claim not supported (`claim_relation_unsupported`) | AB1 |
| F5 | Prevented by `subject_changed` | AB1 |
| F6 | Look `insufficient`, frame inspected | AB2 |
| F7 | Look `unavailable` | AB3 |
| F8 | Supported claim that fails a field rule | AB1 |
| F9 | Two statements prevented in one turn, neither eligible for historical | one abstention, then silence |
| F10 | Two claims, one eligible for historical and one not | the eligible one as a historical statement; one abstention for the rest |
| F11 | Scene changed to another slide, A is 8 s old, claim supported | historical pattern; it states what the checked frame listed and nothing about now |
| F12 | Camera claim, supported, any current-state outcome | historical pattern (no current camera pattern in v1) |
| F13 | Historical gather whose stored provenance lacks a mapped capture instant or a look answer | AB1 |

At most one abstention is spoken per turn. A fallback statement is bound, gated,
synthesized, and receipted like any other statement.

A fallback statement needs speech synthesis like any statement. That is a
request to the speech provider, visible in the journal and the ledger; it is not
vision or language-model inference. Catalog entries can be synthesized ahead of
time. Section 14 asks to confirm this reading of I4.

## 11. Negative controls

Each is one deterministic test. The row it references gives the expected result.

Small but substantive change

- NC-01: one digit of a figure changes, thumbnail below the emit threshold (T7). Not `valid` (S7).
- NC-02: one digit changes, at most 2 thumbnail blocks changed (today `minor`, T6). Not `valid` (S7).
- NC-03: title unchanged, one table cell changed, high visual similarity. Not `valid` (S7).
- NC-04: same slide, cursor or highlight moved over the claimed text. Not `valid` (S7).

Clock uncertainty

- NC-05: A age within 0.05 s either side of 5.0 s. `unknown`, never `valid` (S11).
- NC-06: C age within its uncertainty of 5.0 s. `unknown` (S6).
- NC-07: C mapping uncertainty above 0.05 s, or C unmapped. `unknown` (S13).
- NC-08: C capture instant before A, or after `boundary`. `unknown` (S14).
- NC-09: A unmapped at capture, later exchange would map it. Still `unknown` (S12).
- NC-10: C in another clock domain than the gate. `unknown` (S13).

Stale and irregular sampling

- NC-11: confirmation sampling stopped; last exact C is old. Not `valid` (S5, R3).
- NC-12: exact C fresh at the preliminary decision, old at the commit. No handoff (R10).
- NC-13: no confirmation sample ever produced (today's page behavior, T5), A old. `invalid` (S4).
- NC-14: camera heartbeat without a digest record after A. `unknown` (C9).
- NC-15: old frame hashed again with a new hashing time and an unchanged media timestamp. Does not refresh freshness (D1, D2, S17).
- NC-16: sample with a lower sequence arrives late. Ignored for confirmation (D3).
- NC-17: higher sequence with an earlier capture time. `unknown` (D4).
- NC-18: hashing skipped for CPU budget. `unknown`; the previous digest is not reused (D6).
- NC-19: shared stream changes resolution; pixels otherwise the same. `unknown` (D7).
- NC-20: digest algorithm version differs between A and C. `unknown` (D7).

Scene, source, and subject

- NC-21: another slide shown. `invalid` (S8).
- NC-22: share stopped and restarted with the identical slide. `content` claim valid; `source` claim `unknown` (P2).
- NC-23: another presenter shares the identical slide. `content` claim valid; `source` claim `unknown`; a claim about who presents is decided by the state gate (P3).
- NC-24: content left and returned in the same share. `content` claim valid; `source` claim `unknown` (P4).
- NC-25: screen claim with no declared dependence, after a restart with identical content. `unknown` (N4, P2).
- NC-26: camera stream replaced, same participant, identical frames. `invalid` (C5).
- NC-27: camera sample from another participant, identical frames. `invalid` (C6).

Camera motion

- NC-28: same person, pose change. `unknown`, historical statement (C4).
- NC-29: same person, small motion below the visual threshold. `unknown` (C3).
- NC-30: second person enters the frame. `unknown` (C4).
- NC-31: frozen camera with identical frames and advancing media timestamps. Content and identity hold (C2); spoken with the historical pattern, since v1 has no current camera pattern (F12).
- NC-32: frozen decode: identical frames, media timestamp not advancing. Not `valid` past 5 s (D2).
- NC-33: camera turned off between look and commit. `invalid` (C5).

Missing evidence

- NC-34: digest record missing on A, later sample exists. `unknown` (S18).
- NC-35: declared claim with no evidence item. `unknown` (L2).
- NC-36: look `unavailable`; model still proposed a claim. Not supported; AB3 spoken (A3, F7).
- NC-37: required kind gathered, snapshot missing. `unknown` (`snapshot_missing`, as today).

Commit boundary

- NC-38: distinct sample ingested while the preliminary gate runs, before it reads the store. No handoff (R6).
- NC-39: distinct sample ingested after the preliminary gate read the store, before the commit. No handoff (R7).
- NC-40: similar sample in the same position. No handoff (R7).
- NC-41: exact sample in the same position. Committed valid; decision records the newer sample (R8).
- NC-42: sample ingested in the last step before the commit. No handoff (R7).
- NC-43: sample ingested in the first step after the commit, first write not yet complete. Committed decision unchanged; the sample is recorded as post-commit evidence; the receipt alone says whether audio was written (R11).
- NC-44: sample captured before the commit, ingested after. As NC-43, with the evidence recording that the change preceded the commit (R12).
- NC-45: playback frozen after a valid preliminary decision; scene changes; thaw. No handoff (R15).
- NC-46: there is no suspension point between the final evaluation and the handoff, asserted by a writer fake that records the store revision at handoff and by a scheduler probe that would run in any suspension.
- NC-47: change after the first write completed. The decision is not rewritten (I7).
- NC-48: committed valid, caller cancelled before the first write completes. Receipt `dropped_before_audio`; not counted; no twin speech (R17).
- NC-49: committed valid, first write raises. Zero local audio on the receipt; not counted; no twin speech (R18).
- NC-50: committed valid, clip yields zero bytes. Not counted (R19).
- NC-51: writer needs a wait before taking a frame; a distinct sample is ingested during that wait. No handoff (R20).
- NC-52: a committed `valid` decision with no receipt reporting local audio is never reported as published by measurement.
- NC-53: post-commit evidence is linked to the publication ID and carries its position relative to the commit and to the first write's completion.

Abstention, both directions

- NC-54: each catalog entry, each language, under its condition: spoken, counted once, one receipt with local audio (A1 to A3).
- NC-55: AB2 is never selected when no frame was inspected (A4).
- NC-56: abstention binding with a catalog entry followed by a factual clause. Prevented (A8).
- NC-57: model writes a negative current claim ("nothing on the screen") after an insufficient look. Never synthesized; AB2 spoken (A6).
- NC-58: model writes a hedged claim ("not sure, but …"). Never synthesized (A7).
- NC-59: abstention binding one word away from a catalog entry. Prevented (A10).
- NC-60: model declares a scope. Ignored (A11).
- NC-61: two abstentions in one turn. One spoken (F9).
- NC-62: abstention binding naming an unknown catalog version. Prevented (A15).

Render-only visual speech

- NC-63: "Revenue is $1.2M and growing fast." with a supported revenue claim. The synthesized text and the PCM-bearing statement contain the rendered pattern only; "growing fast" appears nowhere in what is synthesized (M3, M11).
- NC-64: "Revenue is definitely $1.2M." The word "definitely" is never synthesized (M11).
- NC-65: "Revenue is $1.2M right now." The phrase "right now" is never synthesized; the current pattern is spoken only if the commit passes (M11, M1).
- NC-66: model writes "Costs are not approved"; the look says "Costs: approved". The negation is never synthesized and the claim is not supported (L6, M11).
- NC-67: in a visual turn, no model-written sentence is ever passed to synthesis, asserted on the synthesizer fake's inputs across every case in this section.
- NC-68: claim field containing a second sentence. `claim_render_unsupported`; AB1 (G2, F8).
- NC-69: claim field containing a temporal or certainty marker. `claim_render_unsupported` (G4).
- NC-70: claim field longer than the limit. `claim_render_unsupported` (G3).
- NC-71: claim field that is not a span of the look's answer, or is a span of another clause. `claim_render_unsupported` (G1).
- NC-72: value that does not match its type grammar. `claim_render_unsupported` (G5).
- NC-73: negated or conditional claim that the look supports. `claim_render_unsupported`; AB1 (G6).
- NC-74: claim of a shape outside the three. `claim_render_unsupported`; AB1 (G7).
- NC-75: turn language with no pattern. `claim_render_unsupported`; AB1 (G7).
- NC-76: current pattern text is never synthesized for a statement whose commit did not return `valid`; a prevented current statement leaves zero PCM.
- NC-77: every rendered statement records pattern ID, version, and field span offsets, and re-rendering from that record reproduces the spoken text byte for byte (G8).
- NC-78: the same claim declared twice is spoken once (M9).
- NC-79: visual turn where every model sentence is undeclared. One abstention, nothing else (M5, M12).
- NC-80: neutral model sentence in a visual turn. Never synthesized (M14).

Historical statements

- NC-81: current statement prevented by age, A 12 s old. The historical pattern is spoken and contains no present-tense assertion and no model text (F1).
- NC-82: model sentence that begins with "In the frame I checked," or "The checked frame listed". Discarded like any model text; it is not bound as `historical` (I11).
- NC-83: a `historical` binding whose text is not a rendered pattern or AB2. Prevented.
- NC-84: A is 30 s old within uncertainty. Abstention (F3).
- NC-85: fresh exact confirmation sample, A is 40 s old. Abstention; the 30 s is measured from A (H4).
- NC-86: prevented by `subject_changed`. Abstention (F5).
- NC-87: two claims, one eligible. One historical statement and one abstention (F10).
- NC-88: historical statement makes zero vision or language-model calls and has its own publication ID, receipt, and link to the prevented publication.
- NC-89: historical gather with complete provenance. Rendered with the historical pattern; no freshness check; support still required (M13).
- NC-90: historical gather with an unsupported claim. Abstention; the scope `historical` is not assigned (M13).
- NC-91: historical gather missing a mapped capture instant. Abstention (F13).

Claim support

- NC-92: two claims, screen valid and camera unknown. Decided separately; the camera claim cannot ride on the screen claim (M8).
- NC-93: figure that is not in the look's answer. `unknown` (L5).
- NC-94: look says "Costs $1.2M"; claim says revenue is $1.2M. `unknown` (L4).
- NC-95: look says "not approved"; claim says approved. `unknown` (L6).
- NC-96: look says "pending approval"; claim says approved. `unknown` (L6).
- NC-97: "Everything has been approved", no claim declared, no specific token. Discarded; `claim_undeclared` (M5).
- NC-98: claim declared against the wrong evidence kind. `unknown` (L3).
- NC-99: clause with two figures and one attribute. `unknown` (L7).
- NC-100: model marks a claim non-visual in a visual turn. Ignored (M10).
- NC-101: first statement prevented, second independent and valid. Second spoken (M8).

Preserved behavior

- NC-102: every `test_publication_activation.py` case that passes today still passes, except the four `test_finding_*` cases, which flip to their new expected rows (S3, R2, A2, C3), and cases that asserted the model's own sentence was spoken in a visual turn, which now assert the rendered statement.
- NC-103: no production module builds a publication context; both audio paths default to none.
- NC-104: the gate, the commit evaluation, the revalidator, the renderer, and every fallback make zero vision or language-model calls, asserted on the fake providers' call counts.
- NC-105: A's snapshot, capture instant, digest record, and provider attempt on the binding are byte-equal before and after any number of confirmation samples (I2).
- NC-106: a turn with no visual needs publishes the model's text exactly as today (M7).

## 12. Locked decisions

From the reviews of revisions 1 and 2.

| # | Topic | Decision |
| --- | --- | --- |
| 1 | Digest stability | Level 1 is kept as written. Whether a static shared slide is pixel-stable on a real Meet page is unknown and is an activation prerequisite. If it is not stable, similarity is not promoted to compensate; correct speech stays silent until a new representation has its own contract |
| 2 | Digest location | In the page, from decoded pixels at native resolution, no resize, no lossy transform. The record carries dimensions, pixel format, algorithm and version, sequence, source identity, capture time, and uncertainty (section 4) |
| 3 | Level 2 for `general` claims | Not allowed. Similarity never yields `valid` |
| 4 | Historical framing | No blind prefix. A deterministic pattern filled from structured, supported claims; otherwise abstention |
| 5 | Historical age limit | 30 s, measured conservatively from A's capture with its uncertainty, never from a confirmation sample |
| 6 | `invalid` to historical | Conditional: freshness and scene-change reasons only, the original observation proven, the historical statement still true, and no subject attribution problem. `subject_changed` gets no automatic fallback |
| 7 | Abstention classification | Catalog only. Whitespace and case normalization; no fuzzy or semantic matching. Scope is assigned by the runtime |
| 8 | Specific tokens | A negative-control heuristic, not proof of support. Support needs entity, attribute, value, polarity, and status matching. An undeclared visual claim is never published |
| 9 | Camera claims | Accepted that they are spoken in historical framing |
| 10 | Catalog wording | Abstention catalog v1 (section 7) with its conditions of use |
| 11 | Neutral speech | No model-written neutral speech in a visual turn until a reviewed neutral catalog exists |
| 12 | Text beyond the claims | Render-only. Every visual factual statement, current or historical, is rendered by the runtime from verified claims. Raw model wording is never published as a visual factual sentence. A claim that cannot be rendered leads to a safe historical statement or an abstention, never to raw text |
| 13 | Existing historical gathers | Same support verification and historical renderer. The `historical` scope is assigned only after the rendered statement exists with sufficient provenance; otherwise abstention |
| 14 | Renderer patterns | Three claim shapes with the historical wording v1 in section 9. No raw model sentence fallback |
| 15 | Matcher evaluation | Required offline, from existing cassettes read-only, before activation, with no new provider call (section 13) |
| 16 | CPU budget for hashing | Numerically open. D6 fails closed. The number is set after a real measurement that needs its own authorization |

Commit versus write, also locked: a committed decision authorizes an attempt;
local audio is proven only by the playback receipt; post-commit samples are
evidence, not decision input; the implementation must provide an atomic
acceptance boundary (W1 or W2) and may not assume `await writer.write()` is one.

Not authorized by any of these: changing `perception.js`, measuring on a real
Meet page, implementation, or activation.

## 13. Offline evaluation of the support matcher

A prerequisite for activation, to be done in a later offline PR. Existing
cassettes are read-only input. Labels are a new artifact; no cassette, dataset,
ledger, or fixture is modified. No provider call.

| Metric | Requirement |
| --- | --- |
| False support | 0 on the labelled negative-control set. This is a result on that set, not evidence of zero risk in general |
| False rejection | Reported: how many correct answers are silenced |
| Coverage | Share of claims that can be extracted and rendered safely |
| Abstention rate | Reported separately for missing evidence, ambiguous relation, and unsupported shape |
| Language | English and Indonesian measured and reported separately |

## 14. Points to confirm

These came up while applying the review. None of them changes a locked decision;
the tables use the stated default.

1. **Placeholders in the historical patterns.** The review's wording reached this
   note without placeholder names ("The checked frame listed  for  as ."). They
   are filled here as `[attribute]`, `[entity]`, `[value]`, `[status]`, and
   `[item]`, following the review's own current example ("lists revenue for
   Project A as $1.2M"). Please confirm the order, in both languages.
2. **Current patterns.** Only the English entity–attribute–value wording was
   given. The other two shapes and all Indonesian current wording in section 9
   are proposed and need approval.
3. **Camera claims have no current pattern in v1.** Default: historical only.
4. **Field length limit (G3).** Proposed: 8 words and 80 characters per field.
5. **Speech synthesis for fallback statements.** A fallback needs synthesis, which
   is a request to the speech provider. Default reading: I4 forbids vision and
   language-model requests, and ordinary synthesis of runtime text is allowed and
   ledgered. If synthesis must not be added either, fallback is limited to
   catalog entries synthesized ahead of time.
6. **Which writer design.** W1 needs a change to the writer protocol; W2 needs
   the engine to separate the writer's wait from the handoff. Default: decide in
   the implementation PR for the commit, with NC-46 and NC-51 as the acceptance
   tests either way.

## 15. Not in this note

No code, no tests, no settings. No change to the 5 s limit. No participant
observer, consent capture, or device clock exchange. No live measurement. No
activation.

A possible split into small PRs, for discussion only and not authorized here:
(1) failing regression test for T6, and the digest record and match-level types;
(2) confirmation samples and the screen table; (3) commit evaluation and the
atomic acceptance boundary; (4) abstention catalog and its digest check;
(5) identity conditions and the camera table; (6) claim proposal and the support
matcher, with its offline evaluation; (7) renderer, patterns, and render-only
visual turns; (8) historical statements and fallback; (9) audit extension over
every row.
