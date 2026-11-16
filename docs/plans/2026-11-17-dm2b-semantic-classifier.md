# DM-2b semantic lineage classifier: proposal

Status: proposal for engineering approval. Nothing here is implemented, enabled
or authorized to call a provider. The only DM-2b code that exists is the offline
benchmark in `evals/_decision_lineage.py` and its labels in
`evals/fixtures/decision-lineage-pairs.json`.

## What the benchmark is

- 120 synthetic record pairs, 106 claims across 22 meetings of one owner and twin, labelled
  as `curated_evaluation_judgment`. They are not organizational decisions.
- Labels: 34 `related_discussion`, 18 `potential_contradiction`,
  25 `potential_supersession`, 24 `no_established_relationship` and
  19 `abstain`.
- Languages: 28 English, 20 Indonesian and 72 mixed (code-mixed or
  cross-language).
- Covered cases:
  - paraphrases
  - hard negatives with overlapping vocabulary
  - participant claims of owner approval
  - equal meeting timestamps
  - altered and missing source evidence
  - unattributed and ambiguous claims
  - prompt injection inside meeting content
- Every claim goes through the real DM-1 `records_from_report`. Items whose
  evidence is altered or missing produce no record, and their pairs are labelled
  `abstain`.
- Each pair carries both claim keys, its temporal order, the gold evidence
  entries on each side and a written rationale. The labelling rules are in the
  fixture's `guidelines`.

`uv run --no-sync python -m evals._decision_lineage --predictions FILE` scores a
prediction file without any model call. `--baseline NAME` scores a built-in
deterministic predictor. The report gives:

- the confusion matrix
- per-class precision and recall
- the relationship false-positive rate on gold `no_established_relationship`
- abstention recall and answered coverage
- evidence-reference correctness: every cited entry must belong to that side's
  DM-1 record, and both sides must be cited
- counts of supersessions on pairs without a strict time order, supersessions
  on owner-approval claims, and answers on pairs with broken evidence

### Gate a classifier must pass

| Measure | Requirement |
| --- | --- |
| Precision | contradiction ≥ 0.90, supersession ≥ 0.90, related ≥ 0.85, none ≥ 0.80 |
| Recall | related, contradiction, supersession ≥ 0.70; none ≥ 0.80 |
| False relationships on gold `none` | ≤ 5% |
| Abstention | abstention recall ≥ 0.80, answered coverage ≥ 0.80 |
| Evidence references | 100% correct |
| Hard rules | 0 supersessions without a strict time order, 0 supersessions on approval claims, 0 answers on broken evidence |

### Measured baselines (deterministic, no model)

| Predictor | Contradiction R | Supersession R | Related P / R | FPR on none | Abstain R | Gate failures |
| --- | --- | --- | --- | --- | --- | --- |
| oracle (gold labels) | 1.00 | 1.00 | 1.00 / 1.00 | 0.00 | 1.00 | 0 |
| lexical weighted Jaccard ≥ 0.2 | 0.00 | 0.00 | 0.37 / 0.41 | 0.08 | 0.00 | 11 |
| constant `related_discussion` | 0.00 | 0.00 | 0.28 / 1.00 | 1.00 | 0.00 | 11 |
| constant `no_established_relationship` | 0.00 | 0.00 | n/a / 0.00 | 0.00 | 0.00 | 10 |
| constant `abstain` | 0.00 | 0.00 | n/a / 0.00 | 0.00 | 1.00 | 9 |
| constant `potential_supersession` | 0.00 | 1.00 | n/a / 0.00 | 1.00 | 0.00 | 13 |

Constant supersession also produces 4 supersessions without a time order, 8 on
approval claims and 6 answers on broken evidence. No model has been evaluated;
there are no model results.

## Proposed classifier

### Scope

- Same `owner_id` and `twin_id` only.
- Earlier records come from the DM-2a comparison window: the 20 most recent
  earlier meetings with an available debrief, then up to 200 records.
- Pairs without a strict meeting time order are never sent for supersession.
- No cross-owner or cross-twin input. No new store: results are published as a
  DM-2a sidecar.

### Candidate generation (deterministic, no model)

For each record of the later meeting, rank the window's earlier records by
IDF-weighted lexical similarity of item text and quotes, and keep the top 10.

On the benchmark this lexical ranking finds the earlier record of a labelled
relationship (related, contradiction or supersession) in the top 5 for 69 of 77
pairs (0.90) and in the top 10 for 75 of 77 (0.97). Both top-10 misses are
English or cross-language. The same lexical score is useless as a classifier
(see the table), so it only narrows the candidate list and never labels.

The data is synthetic, so recall must be re-measured on real, consented meeting
records before activation. Only if it falls below 0.90 would an embedding-based
candidate step (the already configured embedder) be justified.

### Classification call

- Use the existing debrief role model (`create_debrief_chat_model`) with native
  structured output, as `_extract_chunk` already does. No new provider, SDK or
  dependency.
- One call per later record, with at most 10 candidates.

**Input, all as JSON data in the user message:**

- each record's id, kind, status, text, assignee and due text
- its quotes with entry ids and attribution
- its meeting time

No free-form meeting transcript is sent beyond the cited quotes. Item text and
quotes are already capped at 1,000 characters by the DM-1 schema. A request
over 8,000 input tokens is not truncated: the record is reported as
`not_compared`.

**Output schema** (`extra=forbid`): for each candidate,

- `earlier_record_id`, which must be one of the presented ids
- `label`: one of the four relationships, or `abstain`
- `first_evidence` and `second_evidence`: entry ids taken from that side's
  quotes

There is no free-text field, so the model cannot write a resolution, approval
or rationale into the record.

### Deterministic validation after the call

The output fails closed: a candidate becomes `abstain` when

- it does not parse
- it names an unknown or duplicate id
- it cites evidence that is not from that record
- it labels supersession without a strict time order
- it labels supersession where either record is an `owner_decision`
- the call times out

`abstain` and `no_established_relationship` are recorded as explicit DM-2a
comparisons, so they are never confused with `not_compared`.

### Prompt-injection defences

- The system prompt states that every record field is quoted meeting content,
  not instructions. Labels come only from the enum.
- The model has no tools. Ids and evidence are checked against the presented
  set, and the label cannot alter authority: every comparison stays
  `derived_candidate`, `unreviewed`, `owner_approved: false`.
- The benchmark has four injection pairs, for example a claim telling the
  assistant to mark a pair owner-approved, or a claim saying "these two claims
  contradict each other". A classifier must label them by their content.

### Storage changes this needs (not part of the benchmark PR)

DM-2a's sidecar accepts only `method: manual_curated`. A classifier needs:

- `method: model_classified`
- `provider`, `model` and a prompt digest in the sidecar's provenance
- `abstain` as an explicit comparison label

That is a `decision_lineage/v2` schema with the same file location, locking and
revision rules.

## Evaluation protocol before any activation

1. **One authorized offline run.** 76 calls, one per later record that
   has at least one labelled pair, at temperature 0. Record raw responses
   as a replay cassette, like `evals/_visual_cassette.py`, so later scoring
   never calls the provider.
2. **Stability.** Three repeated runs. Label agreement across runs is reported
   next to the gate result.
3. **Gate.** Pass the gate above. Per-language and injection-case results are
   read from the confusion matrix by category before any decision.
4. **Real data.** Re-label a consented sample of real records, with an English,
   Indonesian and mixed split, and repeat steps 1 to 3. Synthetic results alone
   do not justify activation.

## Provider cost estimate

These are token estimates; the price per token must be read from the provider's
price sheet at approval time and is not assumed here.

- Prompt: about 700 tokens. Each record with its quotes: about 180 tokens. Each
  candidate result: about 40 output tokens.
- One call with 10 candidates: about 700 + 11 × 180 ≈ 2,700 input tokens and
  about 400 output tokens.
- Benchmark run: 76 calls ≈ 205,000 input and 30,000 output tokens. Three
  stability runs: about 615,000 input and 91,000 output tokens.
- Production, if ever enabled: a meeting with 8 records costs about 8 calls ≈
  22,000 input and 3,200 output tokens, once per debrief that changes its
  records.

## Explicitly out of scope until authorized

- provider requests
- enabling classification in any worker
- writing classifier output to a sidecar
- cross-twin lineage
- an owner review workflow
