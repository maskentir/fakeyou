# Publication Safety & Liveness Contract — Phase 5A Hardening

Base: `main` at `ac75933e`. Design only. This note changes no source, test,
setting, dependency, dataset, cassette, or ledger. `PublicationContext` stays off.
Phase 5B is not authorized. 0 provider requests, US$0.

The readiness sprint (#200 to #204) left the gate correct but unusable: it stops
wrong speech and also stops correct speech. Reading the code for this note also
found a path where wrong speech can pass. This note specifies the behavior that
closes both, as decision tables and negative controls that an implementation must
satisfy. Every row is meant to become a deterministic test.

Revision 2 applies the review of revision 1 (`e967419`): historical fallback is
rendered from structured claims and never from the model's sentence; a visual
sentence without declared claims is `unknown`; exact content is separated from
source and subject identity; the boundary before the first PCM write has an
explicit commit rule. The ten decisions from that review are locked in section 12.

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

T6 and T7 mean a small substantive change, for example one digit, can pass
today's gate as unchanged. This is a potential safety bug that needs a regression
test. The first implementation PR should add the failing test for T6 before
changing anything. T10 is why section 10 does not let model-written text into
the `historical` scope.

## 2. Invariants

These hold in every table below. A row that would break one is wrong.

| # | Invariant |
| --- | --- |
| I1 | The capture freshness limit stays 5.0 s, the validation age limit 0.25 s, and the accepted clock uncertainty 0.05 s |
| I2 | The capture time, snapshot identity, provider attempt, and inference of the analyzed frame are immutable. No later sample overwrites them |
| I3 | A later sample has its own identity, capture time, and mapping uncertainty. It is evidence of content continuity, never of the claim itself, and never of source or subject identity |
| I4 | The gate makes no provider request. Nothing in this contract adds one, including re-reading a frame or re-asking the reply model |
| I5 | When support for a claim cannot be proven the decision is `unknown`, never `valid` |
| I6 | `invalid` outranks `unknown`, which outranks `valid`, across the claims of a sentence and the evidence of a claim |
| I7 | A clip's decision is final at its commit point (section 6). The commit point is the last step before the first PCM write. A clip whose committed decision is not `valid` writes no PCM. After the commit point the gate never retracts or rewrites the decision |
| I8 | A prevented sentence is never counted as spoken and never enters the meeting record as twin speech |
| I9 | Nothing here is on by default. Activation needs its own review |
| I10 | The scope of a sentence is assigned by the runtime from deterministic rules. A scope, class, or support declared by a model is input to be checked, never the result |
| I11 | Model-written sentence text is published only in the `current` scope, or in `no_visual` for a turn with no visual needs. Text in the `historical` scope and catalog text are produced by the runtime |

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
use" is.

**Linearization.** Samples reach the gate in one order: the order in which the
runtime ingests them into the perception store. That order, not capture time, is
what the gate can see.

1. **Preliminary decision.** The asynchronous gate reads the store and returns a
   decision together with, per bound source, the sequence of the latest sample it
   read. It has the existing 0.25 s timeout.
2. **Waits.** The engine may still wait after that: thaw, queue, writer
   readiness.
3. **Commit point.** Immediately before the first PCM write, with no suspension
   point between the check and the call to the writer, the engine re-evaluates
   the binding synchronously against the store as it is then. Digest comparison
   is local and constant-time, so this needs no await and no provider. The
   result of this evaluation is the committed decision.
4. **After the commit point.** Publication has started. A sample ingested after
   the commit point does not change the decision and does not make the gate stop
   the clip, including while the first write is still pending in the writer.

A change the sensor has not delivered cannot be seen by the gate. The exposure is
one confirmation interval plus delivery delay. It is reported after the fact by
post-publication continuity, never repaired by rewriting the decision.

| # | Situation | Committed decision | Reason | Fallback |
| --- | --- | --- | --- | --- |
| R1 | Reply ready within 5 s of A, no later sample | valid | `ok` | none |
| R2 | Reply ready after 5 s, latest sample exact and fresh | valid | `continuity_confirmed` | none |
| R3 | Reply ready after 5 s, no fresh sample (sampling stale or stopped) | invalid | `confirmation_too_old` or `capture_too_old` | historical |
| R4 | Reply ready after 5 s, latest sample similar | unknown | `content_changed_unverified` | historical |
| R5 | Reply ready after 5 s, latest sample distinct | invalid | `scene_changed` | historical |
| R6 | Non-exact sample ingested while the preliminary gate is running, before it reads the store | per S7 or S8 | per S7 or S8 | historical |
| R7 | Non-exact sample ingested after the preliminary gate read the store, before the commit point | per S7 or S8; no PCM | per S7 or S8 | historical |
| R8 | Exact sample ingested between the preliminary decision and the commit point | valid, with the newer sample recorded | `continuity_confirmed` | none |
| R9 | Preliminary decision valid, commit point more than 0.25 s after the store was last read and no newer exact sample | re-evaluated at commit; if the latest sample is no longer fresh, per S5 or S6 | per S5 or S6 | historical |
| R10 | Latest sample fresh at the preliminary decision, old at the commit point | invalid | `confirmation_too_old` | historical |
| R11 | Non-exact sample ingested after the commit point, before the writer accepted the first frame | valid stands; clip is spoken | `ok`; post-publication continuity reports `scene_not_continuous` | none |
| R12 | Non-exact sample captured before the commit point, ingested after it | valid stands; clip is spoken | as R11, and the measurement records that the change preceded publication | none |
| R13 | Second sentence of the same reply | its own preliminary decision and its own commit point | per table | per table |
| R14 | Preliminary gate exceeds its 0.25 s timeout | prevented, no decision | `unresolved`, as today | abstention |
| R15 | Playback frozen after the preliminary decision, thawed later | commit point is after the thaw; re-evaluated there | per table | per table |
| R16 | Barge-in or stop before the commit point | no commit; receipt is `dropped_before_audio`, as today | n/a | none |

## 7. Contract 3 — Abstention

Three sentence kinds, with different scopes:

| Kind | Scope | Definition | Gate |
| --- | --- | --- | --- |
| Epistemic uncertainty | `no_visual` | Says only what the speaker does or does not know. Asserts nothing about what is or was visible | valid, no freshness |
| Historical observation | `historical` | Refers explicitly, in the past, to the frame that was checked | valid, no freshness; carries the evidence |
| Current-state claim | `current` | Any assertion about what is visible now, positive or negative | Contracts 1, 2, 4, 5 |

Classification is by the runtime (I10):

- A sentence is an abstention only when the runtime itself selected it from the
  catalog below. A model-written sentence whose normalized text equals a catalog
  entry is treated as that entry; normalization is whitespace and case only.
  There is no fuzzy or semantic matching.
- Every other model-written sentence in a turn with visual needs is `current`.
- After a look that did not produce, no `current` sentence can be valid. The
  runtime speaks the catalog entry for that condition instead of a sentence that
  would be prevented. The reply model is not asked again.
- The identity of the failed look (gather attempt, snapshot, provider attempt,
  status) is journaled with the abstention. An epistemic sentence keeps the
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

| # | Sentence | Look | Scope | Decision | Reason |
| --- | --- | --- | --- | --- | --- |
| A1 | AB1, selected by the runtime | insufficient | `no_visual` | valid | `ok` |
| A2 | AB2, selected by the runtime | insufficient, frame inspected | `historical` | valid | `ok` |
| A3 | AB3, selected by the runtime | unavailable | `no_visual` | valid | `ok` |
| A4 | AB2 requested where no frame was inspected | unavailable | runtime selects AB3 instead | valid | `ok` |
| A5 | "I cannot see that on the screen right now." | insufficient | `current` | unknown; AB2 spoken instead | `validation_support_unknown` |
| A6 | "There is nothing on the screen." | insufficient | `current` | unknown; AB2 spoken instead | `validation_support_unknown` |
| A7 | "I'm not sure, but revenue is $1.2M." | insufficient | `current` | unknown; AB2 spoken instead | `validation_support_unknown` |
| A8 | Catalog text followed by a factual clause in the same sentence | any | `current` | per Contracts 1, 2, 5 | the match is on the whole sentence |
| A9 | Catalog text with different spacing or case, model-written | insufficient | as the entry | valid | `ok` |
| A10 | Catalog text with one word changed, model-written | insufficient | `current` | unknown; catalog entry spoken instead | `validation_support_unknown` |
| A11 | Model declares scope `no_visual` or `historical` for its own sentence | any | declaration ignored | per the rules above | n/a |
| A12 | AB1 after a `produced` look whose claim could not be supported | produced | `no_visual` | valid | `ok` (it withholds, it does not claim) |
| A13 | Catalog entry with no publication context (today's default) | any | none | spoken as today | n/a |
| A14 | AB1 in a turn with no visual needs | n/a | `no_visual` | valid | `ok` |

A1 to A4, A9, and A12 to A14 are the "safe speech is not silenced" half. A5 to A8,
A10, and A11 are the "a claim cannot dress as an abstention" half. Both halves
are required of the same implementation.

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
not as current claims. This is accepted.

## 9. Contract 5 — Sentence-level claim support

Today a sentence is bound to the whole gather (T8). This contract binds each
claim in a sentence to the evidence that supports it.

- Claims come from the reply model's structured output in the reply call that
  already exists. No additional request is made. The declaration is untrusted
  (I10) and is checked deterministically.
- In a turn with visual needs, a model-written sentence can be `valid` only
  through declared claims that are each supported. A sentence with no declared
  claims is `unknown`. The absence of numbers or dates proves nothing: "Everything
  has been approved" is a factual claim.
- A sentence is supported only when every claim it needs is `valid`.

**Support of one claim against the look's answer.** Support is textual and
deterministic. It is evidence that the look said this, not proof of meaning.

| # | Condition | Decision | Reason |
| --- | --- | --- | --- |
| L1 | Evidence item exists, look `produced`; entity, attribute, and value each occur in one clause of the look's answer; normalized values are equal; polarity and status agree; the clause relates exactly one value to that attribute | supported; then Contracts 1, 2, 4 and section 8 | `ok` |
| L2 | Evidence item not in the binding | unknown | `claim_evidence_missing` |
| L3 | Declared against evidence of another kind or subject | unknown | `claim_evidence_mismatch` |
| L4 | Value occurs in the answer but with another entity or attribute ("Costs $1.2M" for a revenue claim) | unknown | `claim_relation_unsupported` |
| L5 | Value does not occur in the answer after normalization | unknown | `claim_untraceable` |
| L6 | Polarity or status differs (answer says "not approved", "pending", "no longer"; claim says "approved") | unknown | `claim_polarity_unsupported` |
| L7 | Clause holds several values or several attributes and the pairing is ambiguous | unknown | `claim_relation_ambiguous` |
| L8 | Claim has no entity, attribute, or value | unknown | `claim_incomplete` |
| L9 | Look `insufficient` or `unavailable` | unknown | `validation_support_unknown` |
| L10 | Evidence decision `invalid` | invalid | that reason |
| L11 | Evidence decision `unknown` | unknown | that reason |

Normalization of values, the clause splitter, and the negation and status marker
lists are fixed and versioned per language. A specific-token scan (digits,
currency and percent forms, dates, times, quoted strings) is used only as a
negative control: it can show a sentence says more than it declared, and can
never show support.

**Per sentence.**

| # | Sentence | Decision | Reason |
| --- | --- | --- | --- |
| M1 | All declared claims supported and valid, and the specific-token scan finds nothing undeclared | valid | `ok` |
| M2 | One claim invalid, others valid or unknown | invalid | the invalid claim's reason |
| M3 | One claim unknown, others valid | unknown | the unknown claim's reason |
| M4 | Screen claim valid and camera claim unknown in one sentence | unknown | the camera claim's reason |
| M5 | No claims declared, turn has visual needs, sentence contains a specific token | unknown | `claim_undeclared` |
| M6 | No claims declared, turn has visual needs, no specific token, not a catalog entry | unknown | `claim_undeclared` |
| M7 | No claims, turn has no visual needs | `no_visual`, valid | `ok` |
| M8 | Declared claims do not cover a specific token in the sentence | unknown | `claim_undeclared` |
| M9 | Two sentences, first unknown, second valid | decided separately; the second is spoken | per sentence |
| M10 | Model declares a claim "non-visual" inside a turn with visual needs | declaration ignored; as M5 or M6 | `claim_undeclared` |
| M11 | Claims supported, but the sentence text asserts something the claims do not ("Revenue is $1.2M and growing fast" with one declared claim) | not detectable by rule beyond the token scan; see limits | n/a |

The decision for a sentence lists every claim with its own decision and reason,
so a prevented sentence can be traced to the claim that stopped it.

**Limits, stated plainly.** M6 means that in a visual turn, connective speech
with no claim ("Sure, let me check.") is prevented unless a reviewed catalog of
neutral phrases exists; section 13 asks for that decision. M11 is the reverse
limit: the rules prove the declared claims are supported, not that the sentence
says nothing more. The specific-token scan narrows that gap and does not close
it. Closing it means the model's sentence is not spoken at all and the runtime
renders the claims itself, which is what section 10 already does for historical
speech.

## 10. Fallback

Prevention never triggers a provider request (I4). What may be spoken instead:

| Fallback | When | What is spoken | Binding |
| --- | --- | --- | --- |
| none | committed decision `valid` | the sentence | as bound |
| historical | every condition in H1 to H6 holds | the historical template, filled by the runtime from the claim | new publication ID, scope `historical`, same evidence, link to the prevented publication |
| abstention | historical is not available | AB1, AB2, or AB3 by its condition | new publication ID, the entry's scope, gather attempt kept |
| silence | an abstention was already spoken in this turn | nothing | journaled |

**Historical template v1.**

- EN: "In the frame I checked, [verified historical description]."
- ID: "Pada gambar yang saya periksa, [deskripsi historis yang didukung bukti]."

The description is produced by a deterministic renderer from the claim's fields,
in the past tense, using fixed patterns per claim shape and language. The model's
sentence is never used, in whole or in part, and no prefix is added to it.

| # | Condition for historical fallback |
| --- | --- |
| H1 | The look `produced`, and the claim is supported by L1 |
| H2 | The entity, attribute, and value inserted into the template are spans of the look's answer, not of the model's sentence |
| H3 | A renderer pattern exists for the claim's shape and language. Otherwise abstention |
| H4 | A's capture instant is mapped, and `a` plus A's uncertainty is at most 30 s, measured from A's own capture, never from a confirmation sample |
| H5 | The prevention reason is one of: `capture_too_old`, `confirmation_too_old`, `confirmation_age_threshold_uncertain`, `capture_age_threshold_uncertain`, `content_changed_unverified`, `continuity_unknown`, `scene_changed`, `scene_continuity_unknown`, `source_identity_changed`, `confirmation_time_unmapped`, `confirmation_time_inconsistent` |
| H6 | There is no subject attribution problem: never after `subject_changed`, `claim_evidence_mismatch`, or any reason from L2 to L8 |

| # | Situation | Spoken |
| --- | --- | --- |
| F1 | Supported revenue claim, prevented by `capture_too_old`, A is 12 s old | historical template with the rendered claim |
| F2 | Same, A is 31 s old | AB1 |
| F3 | Same, A is within its uncertainty of 30 s | AB1 |
| F4 | Claim prevented by `claim_relation_unsupported` | AB1 |
| F5 | Claim prevented by `subject_changed` | AB1 |
| F6 | Look `insufficient`, frame inspected | AB2 |
| F7 | Look `unavailable` | AB3 |
| F8 | Supported claim, no renderer pattern for its shape | AB1 |
| F9 | Two prevented sentences in one turn, neither eligible for historical | one abstention, then silence |
| F10 | Sentence with two claims, one eligible for historical and one not | AB1; a sentence is not partly rendered |
| F11 | Scene changed to another slide, A is 8 s old, claim supported | historical template; it states what the checked frame showed and nothing about now |

At most one abstention is spoken per turn. A fallback sentence is bound, gated,
and receipted like any other sentence. A historical fallback is checked by the
gate for H1 to H6 at its own commit point; the `historical` scope is no longer
valid unconditionally for runtime-rendered claims.

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
- NC-12: exact C fresh at the preliminary decision, old at the commit point. No PCM (R10).
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

- NC-28: same person, pose change. `unknown`, historical fallback (C4).
- NC-29: same person, small motion below the visual threshold. `unknown` (C3).
- NC-30: second person enters the frame. `unknown` (C4).
- NC-31: frozen camera with identical frames and advancing media timestamps. `valid` (C2).
- NC-32: frozen decode: identical frames, media timestamp not advancing. Not `valid` past 5 s (D2).
- NC-33: camera turned off between look and commit. `invalid` (C5).

Missing evidence

- NC-34: digest record missing on A, later sample exists. `unknown` (S18).
- NC-35: declared claim with no evidence item. `unknown` (L2).
- NC-36: look `unavailable`; model still wrote a claim. `unknown`; AB3 spoken (A3, F7).
- NC-37: required kind gathered, snapshot missing. `unknown` (`snapshot_missing`, as today).

Commit boundary

- NC-38: distinct sample ingested while the preliminary gate runs, before it reads the store. No PCM (R6).
- NC-39: distinct sample ingested after the preliminary gate read the store, before the commit point. No PCM (R7).
- NC-40: similar sample in the same position. No PCM (R7).
- NC-41: exact sample in the same position. PCM written; decision records the newer sample (R8).
- NC-42: sample ingested in the last step before the commit point. No PCM (R7).
- NC-43: sample ingested in the first step after the commit point, writer still waiting on the first frame. PCM written; decision unchanged; post-publication continuity reports the change (R11).
- NC-44: sample captured before the commit point, ingested after. As NC-43, with the measurement recording that the change preceded publication (R12).
- NC-45: playback frozen after a valid preliminary decision; scene changes; thaw. No PCM (R15).
- NC-46: there is no suspension point between the commit evaluation and the first write, asserted by a writer that records store revision at entry.
- NC-47: change after the first PCM write. Sentence is spoken; the decision is not rewritten (I7).

Abstention, both directions

- NC-48: each catalog entry, each language, under its condition: spoken, counted once, one receipt with local audio (A1 to A3).
- NC-49: AB2 is never selected when no frame was inspected (A4).
- NC-50: catalog text with a trailing factual clause. Not exempt (A8).
- NC-51: negative current claim ("nothing on the screen") after an insufficient look. Prevented (A6).
- NC-52: hedged claim ("not sure, but …"). Prevented (A7).
- NC-53: near-miss of a catalog entry. Not exempt (A10).
- NC-54: model declares its own scope. Ignored (A11).
- NC-55: two abstentions in one turn. One spoken (F9).

Historical fallback

- NC-56: "Revenue is $1.2M right now." prevented by age. The spoken fallback contains no text of the model's sentence beyond the claim's fields, and no present-tense claim (H2, F1).
- NC-57: model sentence already begins with "In the frame I checked,". It is a `current` sentence and is decided as one (I11).
- NC-58: claim field value that is not a span of the look's answer. No historical fallback (H2).
- NC-59: A is 30 s old within uncertainty. Abstention (F3).
- NC-60: fresh exact confirmation sample, A is 40 s old, sentence prevented for another reason. Abstention; the 30 s is measured from A (H4).
- NC-61: prevented by `subject_changed`. Abstention (F5).
- NC-62: two-claim sentence, one claim not eligible. Abstention, not a partial rendering (F10).
- NC-63: historical fallback makes zero provider calls and has its own publication ID, receipt, and link to the prevented publication.

Sentence-level support

- NC-64: sentence with a valid screen claim and an unknown camera claim. `unknown` (M4).
- NC-65: figure in the sentence that is not in the look's answer. `unknown` (L5).
- NC-66: look says "Costs $1.2M"; claim says revenue is $1.2M. `unknown` (L4).
- NC-67: look says "not approved"; claim says approved. `unknown` (L6).
- NC-68: look says "pending approval"; claim says approved. `unknown` (L6).
- NC-69: "Everything has been approved", no claims declared, no specific token. `unknown` (M6).
- NC-70: figure in the sentence, no claim declared. `unknown` (M5, M8).
- NC-71: claim declared against the wrong evidence kind. `unknown` (L3).
- NC-72: clause with two figures and one attribute. `unknown` (L7).
- NC-73: model marks a claim non-visual in a visual turn. Ignored (M10).
- NC-74: first sentence prevented, second independent and valid. Second spoken (M9).

Preserved behavior

- NC-75: every `test_publication_activation.py` case that passes today still passes, except the four `test_finding_*` cases, which flip to their new expected rows (S3, R2, A2, C3).
- NC-76: no production module builds a publication context; both audio paths default to none.
- NC-77: the gate, the commit evaluation, the revalidator, and every fallback make zero provider calls, asserted on the fake provider's call count.
- NC-78: A's snapshot, capture instant, digest record, and provider attempt on the binding are byte-equal before and after any number of confirmation samples (I2).

## 12. Locked decisions

From the review of revision 1.

| # | Topic | Decision |
| --- | --- | --- |
| 1 | Digest stability | Level 1 is kept as written. Whether a static shared slide is pixel-stable on a real Meet page is unknown and is an activation prerequisite. If it is not stable, similarity is not promoted to compensate; correct speech stays silent until a new representation has its own contract |
| 2 | Digest location | In the page, from decoded pixels at native resolution, no resize, no lossy transform. The record carries dimensions, pixel format, algorithm and version, sequence, source identity, capture time, and uncertainty (section 4) |
| 3 | Level 2 for `general` claims | Not allowed. Similarity never yields `valid` |
| 4 | Historical framing | No blind prefix. A deterministic template filled from structured, supported claims; otherwise abstention (section 10) |
| 5 | Historical age limit | 30 s, measured conservatively from A's capture with its uncertainty, never from a confirmation sample |
| 6 | `invalid` to historical | Conditional: freshness and scene-change reasons only, the original observation proven, the historical statement still true, and no subject attribution problem. `subject_changed` gets no automatic fallback |
| 7 | Abstention classification | Catalog only. Whitespace and case normalization; no fuzzy or semantic matching. Scope is assigned by the runtime |
| 8 | Specific tokens | A negative-control heuristic, not proof of support. Support needs entity, attribute, value, polarity, and status matching. An undeclared visual claim is `unknown` |
| 9 | Camera claims | Accepted that most are spoken in historical framing |
| 10 | Catalog wording | Catalog v1 in section 7 and the historical template in section 10, with their conditions of use |

Not authorized by any of these: changing `perception.js`, measuring on a real
Meet page, implementation, or activation.

## 13. Questions revision 2 raises

These are new and not covered by the ten decisions. Each has a proposed default
that the tables assume.

1. **Neutral speech in a visual turn (M6).** With undeclared sentences `unknown`,
   "Sure, let me check." is prevented. Proposed: accept that for now; a separate
   reviewed catalog of neutral phrases can be added later under the same
   exact-match rule. Until then a visual turn speaks only supported claims,
   historical renderings, and abstentions.
2. **What the sentence says beyond its claims (M11).** Proposed: accept the
   stated limit for `current` sentences in this milestone and record it as an
   activation finding. The stricter alternative is to render every visual
   sentence from claims, as historical speech already is, which removes the
   model's wording from visual answers entirely.
3. **Existing historical gathers (T10).** A question about a previous screen binds
   the model's sentence as `historical` today. I11 says model text is not
   published in that scope. Proposed: such sentences go through section 9 for
   support and are rendered with the historical template, with no freshness
   check.
4. **Renderer patterns.** The first set of claim shapes (entity–attribute–value,
   entity–status, presence of an item) and their EN and ID patterns need approved
   wording, like the catalog.
5. **Evaluating the support matcher.** L1 to L8 are textual rules. Proposed: before
   activation, score them offline against the look answers already in the
   recorded cassettes, with a labelled set of supported and unsupported claims,
   and report false-support count. No provider request is involved.
6. **CPU budget for hashing (D6).** Needs a real-page measurement; not authorized
   now. Proposed: leave the number open and keep D6 as the only behavior fixed.

## 14. Not in this note

No code, no tests, no settings. No change to the 5 s limit. No participant
observer, consent capture, or device clock exchange. No live measurement. No
activation.

A possible split into small PRs, for discussion only and not authorized here:
(1) failing regression test for T6, and the digest record and match-level types;
(2) confirmation samples and the screen table; (3) commit point evaluation;
(4) abstention catalog and substitution; (5) identity conditions and the camera
table; (6) claim declaration and the support matcher; (7) historical renderer and
fallback; (8) audit extension over every row.
