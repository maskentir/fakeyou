# Publication Safety & Liveness Contract — Phase 5A Hardening

Base: `main` at `ac75933e`. Design only. This note changes no source, test,
setting, dependency, dataset, cassette, or ledger. `PublicationContext` stays off.
Phase 5B is not authorized. 0 provider requests, US$0.

The readiness sprint (#200 to #204) left the gate correct but unusable: it stops
wrong speech and also stops correct speech. This note specifies the behavior that
closes that gap, as decision tables and negative controls that an implementation
must satisfy. Every row is meant to become a deterministic test. Where a decision
is still open it is listed in "Open decisions" and the tables state the default
this note proposes.

## 1. What is true on `main` today

Read from the code at `ac75933e`. Rows marked *test* are proven by
`tests/test_publication_activation.py`; rows marked *code* are read from the
source and have no test yet.

| # | Behavior | Source |
| --- | --- | --- |
| T1 | A claim about a slide shown for more than 5 s is `capture_too_old` | test |
| T2 | A claim written more than 5 s after capture is `capture_too_old` | test |
| T3 | A reply after an insufficient look is `validation_support_unknown` | test |
| T4 | Camera scene identity is stream, epoch, and generation; content is not compared | test |
| T5 | The page emits a screen frame only when its 128×72 thumbnail changes. A static slide produces no later sample at all, so there is nothing to confirm continuity with | code: `assets/perception.js` `sampleScreen` |
| T6 | A screen change of at most 2 changed 8×8 thumbnail blocks stays the same segment (`compare_screens` is `minor`), and the bound snapshot identity is the segment's first keyframe. The gate reports `unchanged` for it | code: `perception/frames.py`, `perception/timeline.py`, `perception/context.py` `_screen_snapshot` |
| T7 | A screen change below the page's emit threshold (2 changed thumbnail blocks) is never sent to Python | code: `sampleScreen` |
| T9 | The page emits a camera frame only when at least 12 thumbnail blocks changed or 120 s passed. In between, liveness is a heartbeat that refreshes `seen_at` on the stored frame without any content | code: `sampleCamera`, `CameraFrameStore.touch` |
| T8 | Every sentence of a turn with visual needs is bound to all gathered evidence with one scope. There is no per-claim link | code: `conversation/turn_graph.py` `_bind_publication` |

T6 and T7 mean a small substantive change, for example one digit, can pass
today's gate as unchanged. This is a safety gap, not only a liveness gap. The
first implementation PR should add the failing test for T6 before changing
anything.

## 2. Invariants

These hold in every table below. A row that would break one is wrong.

| # | Invariant |
| --- | --- |
| I1 | The capture freshness limit stays 5.0 s, the validation age limit 0.25 s, and the accepted clock uncertainty 0.05 s |
| I2 | The capture time, snapshot identity, provider attempt, and inference of the analyzed frame are immutable. No later sample overwrites them |
| I3 | A later sample has its own identity, capture time, and mapping uncertainty. It is evidence of continuity, never of the claim itself |
| I4 | The gate makes no provider request. Nothing in this contract adds one, including re-reading a frame or re-asking the reply model |
| I5 | When support for a claim cannot be proven the decision is `unknown`, never `valid` |
| I6 | `invalid` outranks `unknown`, which outranks `valid`, across the claims of a sentence and the evidence of a claim |
| I7 | The decision is taken once, before the first PCM write, and a clip that is not `valid` writes no PCM |
| I8 | A prevented sentence is never counted as spoken and never enters the meeting record as twin speech |
| I9 | Nothing here is on by default. Activation needs its own review |

## 3. Vocabulary

**Analyzed capture (A).** The frame a provider look inspected. It carries the
snapshot reference, the mapped capture instant with its uncertainty, the provider
attempt, and the look status (`produced`, `insufficient`, `unavailable`).

**Confirmation sample (C).** A later local sample of the same source and subject,
taken with no provider request. It carries its own snapshot reference, its own
mapped capture instant and uncertainty, and a content digest with the digest
method named.

**Match level** between A and C:

| Level | Meaning | May extend a claim |
| --- | --- | --- |
| `exact` | Level 1. The content digests are equal, where the digest is over the bytes or over a deterministic lossless normalized representation (decoded pixels in a fixed size and color space). Two samples with different frame identity can match exactly | yes |
| `similar` | Level 2. Not exact, but below the existing visual change threshold. The 128×72 blurred thumbnail signature is a `similar` test, not an `exact` one, because it is lossy | no |
| `distinct` | Above the visual change threshold, or another subject, stream, epoch, or generation | no |
| `unknown` | A digest, method, or sample is missing, or the methods differ | no |

**Claim class.** `specific` is a number, amount, date, time, name, status, quoted
text, table cell, or chart value. `general` is everything else. A claim that is
not classified is `specific`.

**Times.** `boundary` is the gate's decision time. `a` is `boundary` minus A's
capture instant, `c` is `boundary` minus C's capture instant, `u` is the mapping
uncertainty of the sample in question. "Fresh" means age plus `u` is at most
5.0 s. Age minus `u` above 5.0 s is "old". Between the two is "uncertain".

## 4. Contract 1 — Static slide (screen continuity)

A screen claim may be published from an analyzed capture older than 5 s only when
a fresh confirmation sample matches it exactly. The analyzed capture keeps its
own time; the decision records both.

Requirement on sampling, with no provider request: the screen source must produce
a confirmation sample at a bounded interval even when nothing changed, carrying
the digest and capture instant but not necessarily the image.

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
| S9 | produced | any | C | unknown | any | unknown | `continuity_unknown` | historical |
| S10 | produced | any | share stopped | n/a | n/a | invalid | `scene_changed` | historical |
| S11 | produced | uncertain | none | n/a | n/a | unknown | `capture_age_threshold_uncertain` | historical |
| S12 | produced | unmapped at capture | any | any | any | unknown | `capture_time_unmapped` | abstention |
| S13 | produced | any | C unmapped or `u` above 0.05 s | exact | n/a | unknown | `confirmation_time_unmapped` | historical |
| S14 | produced | any | C captured before A | exact | n/a | unknown | `confirmation_precedes_capture` | historical |
| S15 | produced | old | exact C, then a later similar or distinct sample | n/a | n/a | as S7 or S8 | as S7 or S8 | historical |
| S16 | produced | old | distinct sample, then exact C (slide left and came back) | exact | fresh | valid | `continuity_confirmed` | none |

Notes that are part of the contract:

- "Latest sample" is the newest sample of that source at `boundary`. An older
  exact match does not outvote a newer non-exact sample (S15).
- S7 tightens today's behavior (T6): a `similar` sample no longer counts as
  unchanged, whatever the age of A.
- S16 is valid because the claim is about content, and the content is proven
  present now. The audit records that the scene was not continuous in between.
- A's capture instant is mapped once, when A is captured, and stored. It is never
  re-derived later, so the 120 s mapping span no longer turns a long-lived slide
  into `capture_time_unmapped`. If it could not be mapped at capture it stays
  unmapped (S12).
- The decision carries A's reference and C's reference, both capture instants,
  both uncertainties, the match level, and the digest method.

## 5. Contract 2 — Slow reply (use-time confirmation)

The limit stays 5 s. A reply that arrives late is published only when a fresh
confirmation sample proves the content is still what was analyzed. This is the
same rule as Contract 1, applied at the moment of use; the table adds the timing
cases specific to the gap between the decision and the first PCM write.

| # | Situation | Decision | Reason | Fallback |
| --- | --- | --- | --- | --- |
| R1 | Reply ready within 5 s of A, no later sample | valid | `ok` | none |
| R2 | Reply ready after 5 s, fresh exact C | valid | `continuity_confirmed` | none |
| R3 | Reply ready after 5 s, no fresh C (sampling stale or stopped) | invalid | `confirmation_too_old` or `capture_too_old` | historical |
| R4 | Reply ready after 5 s, latest sample similar | unknown | `content_changed_unverified` | historical |
| R5 | Reply ready after 5 s, latest sample distinct | invalid | `scene_changed` | historical |
| R6 | Decision valid, then a non-exact sample arrives before the first PCM write | prevented | `scene_changed` or `content_changed_unverified` | historical |
| R7 | Decision valid, first PCM write more than 0.25 s after the validation | unknown | `validation_stale` | historical |
| R8 | Decision valid, then the latest sample ages past 5 s before the first PCM write | unknown | `confirmation_too_old` | historical |
| R9 | Second sentence of the same reply, written later | decided on its own at its own `boundary` | per table | per table |
| R10 | Gate needs more than its 0.25 s timeout | prevented, no decision | `unresolved`, as today | abstention |

Stated limit: a change after the last sample and before the first PCM write
cannot be seen. The exposure is at most one confirmation interval. It is caught
after the fact by post-publication continuity (`scene_not_continuous`), not by
the gate. The contract does not claim otherwise.

## 6. Contract 3 — Abstention

Three sentence kinds, with different scopes:

| Kind | Scope | Definition | Gate |
| --- | --- | --- | --- |
| Epistemic uncertainty | `no_visual` | Says only what the speaker does or does not know. Asserts nothing about what is or was visible | valid, no freshness |
| Historical observation | `historical` | Refers explicitly, in the past, to the frame that was checked | valid, no freshness; carries the evidence |
| Current-state claim | `current` | Any assertion about what is visible now, positive or negative | Contracts 1, 2, 4 |

A model-written sentence is never trusted to declare itself an abstention.
Classification is deterministic:

- A sentence is `no_visual` or abstaining-`historical` only when its normalized
  text equals an entry in a fixed, versioned abstention catalog for its language.
- Every other sentence in a turn with visual needs is `current` (or `historical`
  when the gather itself was historical, as today).
- After a look whose status is `insufficient` or `unavailable`, no `current`
  sentence can be valid (there is no supported claim). The turn speaks the
  catalog abstention for that case instead of a sentence that would be prevented.
  The model reply is not requested again.
- The identity of the failed look (gather attempt, snapshot, provider attempt,
  status) is journaled with the abstention. An epistemic sentence keeps the
  gather attempt ID on its binding and carries no evidence, as the binding
  already requires.

| # | Sentence | Look status | Scope | Decision | Reason |
| --- | --- | --- | --- | --- | --- |
| A1 | "I don't have enough reliable information to answer." (catalog) | insufficient | `no_visual` | valid | `ok` |
| A2 | "I couldn't determine that from the frame I checked." (catalog) | insufficient | `historical` | valid | `ok` |
| A3 | "I cannot see that on the screen right now." | insufficient | `current` | unknown | `validation_support_unknown` |
| A4 | "There is nothing on the screen." | insufficient | `current` | unknown | `validation_support_unknown` |
| A5 | "I'm not sure, but revenue is $1.2M." | insufficient | `current` | unknown | `validation_support_unknown` |
| A6 | "I don't have enough reliable information to answer, but revenue is $1.2M." | any | `current` | per Contracts 1, 2, 5 | catalog match is on the whole sentence |
| A7 | Catalog sentence with different spacing or case | insufficient | `no_visual` | valid | `ok` |
| A8 | Catalog sentence with one word changed | insufficient | `current` | unknown | `validation_support_unknown` |
| A9 | Catalog `historical` abstention after a `produced` look | produced | `historical` | valid | `ok` (it withholds, it does not claim) |
| A10 | Catalog abstention, no publication context (today's default) | any | none | spoken as today | n/a |
| A11 | Catalog abstention in a turn with no visual needs | n/a | `no_visual` | valid | `ok` |

A1, A2, A7, A9, and A11 are the "safe speech is not silenced" half. A3 to A6 and
A8 are the "a claim cannot dress as an abstention" half. Both halves are required
of the same implementation.

## 7. Contract 4 — Camera continuity

Stream identity and liveness prove the camera is on and is the same stream. They
do not prove that what was described is still there. A camera claim needs a
content check.

A live camera never repeats a frame exactly, so Level 1 is rare for cameras. The
contract does not weaken Level 1 to make camera claims pass; it routes them to
historical framing instead.

| # | A status | A age | Latest sample after A | Match | Decision | Reason | Fallback |
| --- | --- | --- | --- | --- | --- | --- | --- |
| C1 | produced | fresh | none | n/a | valid | `ok` | none |
| C2 | produced | fresh or old | C fresh | exact (still image, avatar, frozen virtual camera) | valid | `continuity_confirmed` | none |
| C3 | produced | fresh | C | similar (same person, small motion) | unknown | `content_changed_unverified` | historical |
| C4 | produced | fresh | C | distinct (pose, framing, lighting, another person in frame) | unknown | `content_changed_unverified` | historical |
| C5 | produced | any | camera off, participant left, epoch or generation changed, stream replaced | n/a | invalid | `scene_changed` | historical |
| C6 | produced | any | sample carries another subject | n/a | invalid | `subject_changed` | abstention |
| C7 | produced | old | none, or only non-exact | n/a | invalid | `capture_too_old` | historical |
| C8 | produced | any | no sample within the liveness timeout | n/a | unknown | `scene_continuity_unknown` | historical |
| C8b | produced | any | heartbeat only, no digest | unknown | unknown | `continuity_unknown` | historical |
| C9 | insufficient | any | any | any | per Contract 3 | per Contract 3 | catalog abstention |

C4 is `unknown`, not `invalid`: motion does not show the claim is false ("Budi
wears grey" survives a pose change), it shows the gate cannot prove it. C5 and C6
are `invalid` because the subject of the claim is no longer the scene.

The camera source has the same sampling requirement as the screen: every sample
carries a digest and capture instant, at the existing 4 s interval, instead of
only the heartbeat of T9. A heartbeat without a digest is a sample with match
level `unknown`.

Consequence, stated plainly: once every 4 s sample carries a digest, and with
replies that take several seconds, most camera claims will have a newer non-exact
sample at the gate and will be spoken in historical framing, not as current
claims.

## 8. Contract 5 — Sentence-level claim support

Today a sentence is bound to the whole gather. This contract binds each factual
claim in a sentence to the evidence that supports it.

- A **claim** is one assertion about one visual source, with its class
  (`specific` or `general`) and the evidence item it rests on.
- Claims come from the reply model's structured output in the reply call that
  already exists. No additional request is made. The declaration is untrusted
  and is checked deterministically.
- A sentence is supported only when every claim it needs is `valid`.

Per claim:

| # | Claim | Evidence | Decision | Reason |
| --- | --- | --- | --- | --- |
| L1 | Declared, evidence item exists, look `produced`, value traceable | Contracts 1, 2, 4 give valid | valid | `ok` |
| L2 | Declared, evidence item not in the binding | none | unknown | `claim_evidence_missing` |
| L3 | Declared against evidence of another kind or subject | wrong item | unknown | `claim_evidence_mismatch` |
| L4 | `specific` value (number, date, time, quoted text) that does not occur in the look's answer after normalization | exists | unknown | `claim_untraceable` |
| L5 | Evidence look `insufficient` or `unavailable` | exists | unknown | `validation_support_unknown` |
| L6 | Evidence decision `invalid` | exists | invalid | that reason |
| L7 | Evidence decision `unknown` | exists | unknown | that reason |

Per sentence:

| # | Sentence | Decision | Reason |
| --- | --- | --- | --- |
| M1 | All claims valid | valid | `ok` |
| M2 | One claim invalid, others valid or unknown | invalid | the invalid claim's reason |
| M3 | One claim unknown, others valid | unknown | the unknown claim's reason |
| M4 | Screen claim valid and camera claim unknown in one sentence | unknown | the camera claim's reason |
| M5 | No claims declared, turn has visual needs, sentence contains a `specific` token | unknown | `claim_undeclared` |
| M6 | No claims declared, turn has visual needs, no `specific` token, not a catalog abstention | `current` against all gathered evidence, as today | per Contracts 1, 2, 4 |
| M7 | No claims, turn has no visual needs | `no_visual`, valid | `ok` |
| M8 | Declared claims do not cover a `specific` token in the sentence | unknown | `claim_undeclared` |
| M9 | Two sentences, first unknown, second valid | decided separately; the second is spoken | per sentence |

The decision for a sentence lists every claim with its own decision and reason,
so a prevented sentence can be traced to the claim that stopped it.

## 9. Fallback

Prevention never triggers a provider request (I4). What may be spoken instead:

| Fallback | When | What is spoken | Binding |
| --- | --- | --- | --- |
| none | decision `valid` | the sentence | as bound |
| historical | the look `produced`, every claim is traceable (L1 apart from freshness or continuity), and A's capture instant is mapped | the same content framed as a past observation of the checked frame | new publication ID, scope `historical`, same evidence, link to the prevented publication |
| abstention | historical is not available, or the look did not produce | the catalog epistemic sentence | new publication ID, scope `no_visual`, gather attempt kept |
| silence | abstention already spoken in this turn | nothing | journaled |

At most one abstention is spoken per turn. A fallback sentence goes through the
gate like any other sentence and has its own playback receipt.

## 10. Negative controls

Each is one deterministic test. The ID is the test's subject; the expected result
is in the row it references.

Small but substantive change

- NC-01: one digit of a figure changes, thumbnail below the emit threshold (T7). Not `valid` (S7 via digest mismatch).
- NC-02: one digit changes, at most 2 thumbnail blocks changed (today `minor`). Not `valid` (S7).
- NC-03: title unchanged, one table cell changed, high visual similarity. Not `valid` (S7).
- NC-04: same slide, cursor or highlight moved over the claimed text. Not `valid` (S7).

Clock uncertainty

- NC-05: A age within 0.05 s either side of 5.0 s. `unknown`, never `valid` (S11).
- NC-06: C age within its uncertainty of 5.0 s. `unknown` (S6).
- NC-07: C mapping uncertainty above 0.05 s, or C unmapped. `unknown` (S13).
- NC-08: C capture instant before A, or in the future of `boundary`. `unknown` (S14).
- NC-09: A unmapped at capture, later exchange would map it. Still `unknown` (S12).
- NC-10: C in another clock domain than the gate. `unknown` (S13).

Stale sampling

- NC-11: confirmation sampling stopped; last exact C is old. Not `valid` (S5, R3).
- NC-12: exact C fresh at the decision, old at the first PCM write. Prevented (R8).
- NC-13: no confirmation sample ever produced (today's page behavior, T5), A old. `invalid` (S4).
- NC-13b: camera heartbeat without a digest after A. `unknown` (C8b).

Scene replacement

- NC-14: another slide shown. `invalid` (S8).
- NC-15: share stopped and restarted with the same slide: exact C is valid (S16); with another slide: `invalid`.
- NC-16: another presenter shares the identical slide. Screen claim valid by content; a presenter state claim is decided by the state gate, not this one.
- NC-17: camera stream replaced, same participant. `invalid` (C5).
- NC-18: camera sample from another participant under the same stream ID. `invalid` (C6).

Camera motion

- NC-19: same person, pose change. `unknown`, historical fallback (C4).
- NC-20: same person, small motion below the visual threshold. `unknown` (C3).
- NC-21: second person enters the frame. `unknown` (C4).
- NC-22: frozen camera with identical frames. `valid` (C2).
- NC-23: camera turned off between look and gate. `invalid` (C5).

Missing evidence

- NC-24: digest missing on A or on C, or digest methods differ. `unknown` (S9).
- NC-25: declared claim with no evidence item. `unknown` (L2).
- NC-26: look `unavailable`; model still wrote a claim. `unknown`; catalog abstention spoken (A5, C9).
- NC-27: required kind gathered, snapshot missing. `unknown` (`snapshot_missing`, as today).

Change just before the first PCM write

- NC-28: distinct sample arrives after a valid decision, before the write. No PCM (R6).
- NC-29: similar sample arrives after a valid decision, before the write. No PCM (R6).
- NC-30: change arrives after the first PCM write. Sentence is spoken; post-publication continuity reports `scene_not_continuous`; the gate decision is not rewritten.

Abstention, both directions

- NC-31: each catalog sentence, each language, after an insufficient look: spoken, counted once, one receipt with local audio (A1, A2).
- NC-32: catalog sentence with trailing factual clause. Not exempt (A6).
- NC-33: negative current claim ("nothing on the screen") after an insufficient look. Prevented (A4).
- NC-34: hedged claim ("not sure, but …"). Prevented (A5).
- NC-35: near-miss of a catalog sentence. Not exempt (A8).
- NC-36: model declares scope `no_visual` for a factual sentence. Declaration ignored; decided as `current`.
- NC-37: two abstentions in one turn. One spoken (section 9).

Sentence-level support

- NC-38: sentence with a valid screen claim and an unknown camera claim. `unknown` (M4).
- NC-39: figure in the sentence that is not in the look's answer. `unknown` (L4).
- NC-40: figure in the sentence, no claim declared. `unknown` (M5, M8).
- NC-41: claim declared against the wrong evidence kind. `unknown` (L3).
- NC-42: first sentence prevented, second independent and valid. Second spoken (M9).

Preserved behavior

- NC-43: every `test_publication_activation.py` case that passes today still passes, except the four `test_finding_*` cases, which flip to their new expected rows (S3, R2, A1 or A2, C3).
- NC-44: no production module builds a publication context; both audio paths default to none.
- NC-45: the gate, the revalidator, and every fallback make zero provider calls, asserted on the fake provider's call count.
- NC-46: A's snapshot, capture instant, and provider attempt on the binding are byte-equal before and after any number of confirmation samples (I2).

## 11. Open decisions

These need an explicit answer before implementation. Each has a proposed default
that the tables above already assume.

1. **Is a static shared slide pixel-identical across samples on a real Meet
   page?** Level 1 on decoded pixels assumes yes. Video decoding may add small
   frame-to-frame differences, in which case S3 and R2 never fire and correct
   speech stays silent, safely. This cannot be measured offline, and no live run
   is authorized. Proposed default: specify Level 1 as written, treat the answer
   as unknown, and make "measure digest stability on a real page" an explicit
   activation prerequisite. If it turns out unstable, a new deterministic
   representation needs its own contract; similarity is not to be promoted to
   Level 1 to compensate.
2. **Where is the digest computed?** Proposed: in the page, over full-resolution
   decoded pixels, sent with every sample at the existing 500 ms screen cadence
   without the image. Alternative: send every frame and hash in Python, which
   costs bandwidth. Either changes `perception.js`, which has never run on a real
   Meet page since #201.
3. **May a `general` claim use Level 2?** Proposed: no. Level 2 never yields
   `valid` for any class until a deterministic classifier of claim class exists
   and is reviewed. The class is recorded now so the rule can be relaxed later
   without re-binding.
4. **How is historical framing produced without a request?** Proposed: a fixed
   catalog prefix per language in front of the unchanged sentence, for example
   "As of the frame I checked a moment ago: …". Alternative: the reply model
   emits a past-tense variant of each visual sentence in the same reply call,
   which reads better and costs output tokens and latency on every turn.
5. **Is there an age limit on historical fallback?** Proposed: yes, 30 s from A's
   capture; older than that the fallback is the abstention.
6. **Does an `invalid` decision allow historical fallback?** Proposed: yes for
   `capture_too_old`, `confirmation_too_old`, and `scene_changed`, because the
   past observation is still true; no for `subject_changed`.
7. **Catalog-only abstention, or a classifier?** Proposed: catalog only, with
   deterministic substitution after an insufficient look. It is strict and cannot
   be talked around. The cost is that a model-written abstention outside the
   catalog is replaced by the catalog sentence.
8. **What counts as a `specific` token for M5 and M8?** Proposed: digits,
   currency and percent forms, dates, times, and quoted strings, by a fixed
   pattern set. Names are not detectable by pattern; proposed handling is that a
   name counts as `specific` only when declared, plus a match against the
   participant roster.
9. **Camera claims mostly become historical (section 7).** Proposed: accept this.
   The alternative is a per-attribute stability rule (clothing versus pose), which
   needs a claim taxonomy this note does not define.
10. **Catalog languages and wording.** English and Indonesian entries need
    owner-approved text. Indonesian text lives in JSON, not in `.py`.

## 12. Not in this note

No code, no tests, no settings. No change to the 5 s limit. No participant
observer, consent capture, or device clock exchange. No live measurement. No
activation.

A possible split into small PRs, for discussion only and not authorized here:
(1) failing test for T6 and the digest and match-level types; (2) confirmation
samples and the screen table; (3) use-time recheck before the first PCM write;
(4) abstention catalog and substitution; (5) camera table; (6) claim declaration
and sentence-level decision; (7) fallback; (8) audit extension over all rows.
