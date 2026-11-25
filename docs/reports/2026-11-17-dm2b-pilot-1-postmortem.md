# DM-2b pilot #1 postmortem

**Ruling:** pilot complete; classifier quality NO-GO. No further provider requests are
authorized. Every number below is reproducible offline with
`uv run --no-sync python -m evals._lineage_postmortem rescore`.

## Experiment record

The run used approval `dm2b-pilot-2026-11-17` at `a1a4b71d`, with Google
`gemini-3.8-flash`, Standard tier and thinking LOW. It ran from 02:05:52 to
02:07:33 UTC on 2026-11-17.

| Measure | Value |
| --- | --- |
| HTTP attempts | 21 |
| Failed attempts | 0 |
| Retries | 0 |
| Input tokens | 28,396 |
| Output tokens (including 274 thinking) | 10,402 |
| Accounted spend | $0.0603 |
| Stop reason | completed |

Google billing has not been reconciled against the accounted spend.

The approval, receipt, cassette, live report, replay report and stderr stay
read-only under `.runtime/lineage-pilot/`. Their SHA-256 digests are in
`evals/fixtures/lineage-pilot-1/manifest.json`.

The cassette and the replay report are committed as byte-identical copies, and
`verify` checks both against the manifest. The live report is not committed,
because it contains a local path. Validator v1 reproduces the committed original
report exactly, including its five gate failures.

## What failed, by layer

The 23 pilot pairs split by what happened to them:

| Stage | Pairs |
| --- | --- |
| Broken evidence; deterministic abstain, no call | 091 |
| No strict time order; excluded, no call | 079 |
| Retrieval miss; never shown to the model | 052, 064, 065, 120 |
| Presented to the model | 17 |

The model-only figures use the 17 presented pairs as the denominator. The other
six pairs never reached the model and are excluded.

### 1. Contract defect: negative answers without evidence (fixed here)

All 75 `evidence_invalid` decisions came from the same cause. The model answered
`no_established_relationship` with no evidence, and validator v1 required
evidence for every label except abstain. That turned two correct hard-negative
answers into abstentions: pairs 010 and 034.

Validator v2 and the scorer now apply label-specific rules:

- **Relationships** (related, contradiction, supersession) need valid evidence
  from both records.
- **`no_established_relationship`** may cite nothing. If it cites anything, it
  must cite valid ids on both sides.
- **`abstain`** never carries a relationship or evidence.

Fabricated, cross-side and one-sided citations are still rejected. The decision
keeps the earlier and later record ids it was bound to.

### 2. Semantic failures (genuine; not fixed)

On the 17 presented pairs, the raw model labels compare with the gold labels as
follows:

| Gold \ model | related | contradiction | supersession | none | abstain |
| --- | --- | --- | --- | --- | --- |
| related (4) | 4 | | | | |
| contradiction (4) | | 1 | 3 | | |
| supersession (4) | | | 4 | | |
| none (4) | | | | 4 | |
| abstain (1) | 1 | | | | |

These are the remaining genuine model errors:

- **Pairs 001, 051 and 053:** contradiction labelled as supersession.
  - The later claims ("stays on MySQL for now", "tidak dipindah ke S3", "dipegang
    tim SRE secara bergiliran") reverse or reassign the earlier claim without an
    explicit revision marker.
  - The prompt's definition of supersession ("cancelling … reassigning") and the
    benchmark's definition disagree on these cases.
  - Human adjudication is required. Gold labels and the prompt are unchanged.
- **Pair 060:** an ambiguous claim ("Caching cukup untuk saat ini") labelled as
  related instead of abstain.
  - It is the only abstain pair the model actually saw.
  - Abstention by the model is therefore 0 of 1.

Contradiction recall is 1 of 4 on presented pairs. Supersession precision is 4 of
7, because 3 contradictions were absorbed into supersession.

### 3. Retrieval misses (untested, not model failures)

Lexical ranking inside the development pool never showed the model four of the
pairs:

| Pair | Gold | Window | Rank | Similarity | 10th similarity | Shared tokens |
| --- | --- | --- | --- | --- | --- | --- |
| 052 | abstain | 19 | 17 | 0.0 | 0.0 | none |
| 064 | abstain | 19 | 11 | 0.0 | 0.0 | none |
| 065 | abstain | 48 | 39 | 0.0 | 0.0 | none |
| 120 | none (injection) | 52 | 11 | 0.0141 | 0.0146 | "the" |

- **The three abstain pairs** share no content word with their earlier claim
  ("Hal itu dibahas lagi nanti", "Ikuti keputusan kemarin"). Their rank comes only
  from the record-id tie-break among zero scores.
- **The prompt-injection pair (120)** shares only "the", so its injection
  resistance is untested.
- **What this means:**
  - Leaving these pairs out is the right retrieval behaviour for unrelated or
    vague claims.
  - The pilot design was wrong to count them as classifier tests.
  - Raising top-K would not fix this; it would only move the zero-score tie-break.

### 4. Unlabeled relationships (pending annotation)

The model predicted six relationships on candidate pairs that have no gold label:

| Model label | Earlier claim | Later claim |
| --- | --- | --- |
| supersession | Dimas writes the rollback plan. | Rencana rollback dipegang tim SRE secara bergiliran. |
| supersession | The billing service stays on MySQL for now. | Billing moves to Postgres 17 after all, starting October. |
| related | What will S3 storage cost? | Log billing tetap di Elasticsearch, tidak dipindah ke S3. |
| related | The billing service moves to Postgres 17 this quarter. | Billing go-live slips to December. |
| related | Billing moves to Postgres 17 after all, starting October. | Billing go-live slips to December. |
| related | The billing service stays on MySQL for now. | Billing go-live slips to December. |

None of them counts as correct, incorrect or harmless until adjudicated.

## Original and corrected end-to-end scores (23 pairs)

The corrected column rescores the same recorded responses with validator v2. It is
not a new measurement of the model.

| Metric | Original (v1) | Corrected (v2) |
| --- | --- | --- |
| related P / R | 0.80 / 1.00 | 0.80 / 1.00 |
| contradiction P / R | 1.00 / 0.20 | 1.00 / 0.20 |
| supersession P / R | 0.57 / 1.00 | 0.57 / 1.00 |
| none P / R | 1.00 / 0.40 | 1.00 / 0.80 |
| abstain P / R | 0.50 / 0.80 | 0.67 / 0.80 |
| answered coverage | 0.778 | 0.889 |
| relationship false-positive rate | 0.0 | 0.0 |
| evidence validity | 1.0 | 1.0 |
| `evidence_invalid` decisions | 75 | 0 |
| gate failures | 5 | 3 |

**Gate failures that remain under v2:**

- supersession precision
- contradiction recall
- related precision

All three are semantic failures, not contract failures.

**What the corrected score does not establish:**

- **Abstention:** the 0.80 recall rests on three retrieval misses and one
  deterministic exclusion. It is not evidence that the model abstains.
- **Evidence selection:** sufficiency and precision are 1.0, but only one
  multi-evidence pair (129) exercised selection.

## Human adjudication

`python -m evals._lineage_postmortem packet --out DIR --key-out DIR` writes a
blind packet of 10 items: pairs 001, 051, 053 and 060, plus the six unlabeled
candidates, in a hashed order. The coordinator key is written to a separate
folder.

- **What each item contains:** both claims, their meeting times, kinds and exact
  quotes, and the transcript line each quote comes from.
- **What it leaves out:** pair ids, record ids, gold labels and model output.
- **Labelling rule:** the instructions define supersession by explicit evidence
  of revision in the later claim, not by time order or a different value.
- **Recording results:** `compare --packet FILE --key FILE --responses FILE`
  records each label, the revision evidence, the rationale and disagreements.
  It never edits gold labels.

**Status:** not started. An independent second annotator is still required
before any benchmark-based activation decision.

## Classifier-component suite (offline, not run)

`python -m evals._lineage_postmortem component` builds requests that force each
target pair's earlier claim into the candidate list. It is classifier-only
testing and is reported separately from natural retrieval and end-to-end
results.

| | Value |
| --- | --- |
| Pairs | 20 development pairs |
| Hard negatives | 13 |
| Ambiguous | 7 |
| Prompt injection | 1 |
| Calls | 16 |
| Worst case (offline estimate) | $0.26 |

**Gap:** owner-approval-claim pairs exist only in the frozen partition. The suite
cannot cover them until reviewed development cases are curated.

## What must change before another dollar is spent

1. Merge the v2 evidence contract (this change), after review.
2. Adjudicate items 1 to 10 with at least one human, and recruit the independent
   second annotator.
3. Settle the contradiction/supersession definition from the adjudication. Then
   change the prompt, or the gold labels through `label_corrections`, never both
   silently.
4. Curate development approval-claim cases and add them to the component suite.
5. Only then consider a second pilot.
   - It should be limited to the component suite plus the four presented
     contradiction pairs.
   - It needs a new approval and its own cost proposal.
