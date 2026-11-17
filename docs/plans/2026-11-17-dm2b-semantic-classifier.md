# DM-2b semantic lineage classifier: proposal

Status: proposal for engineering approval. Nothing here is implemented, enabled
or authorized to call a provider. The only DM-2b code that exists is the offline
benchmark in `evals/_decision_lineage.py`, its labels in
`evals/fixtures/decision-lineage-pairs.json` and the blind second-annotation
tooling in `evals/_lineage_annotation.py`.

## What the benchmark is

**Status: synthetic / single-curator / provisional evaluation benchmark.** The
fixture records this as `benchmark_status`, with a `provenance` note. One
curator wrote every claim, label and rationale. No second annotator has
reviewed them, so agreement is unmeasured. The labels are curated evaluation
judgments, not organizational decisions, and corrections to them must be
recorded in `label_corrections` (there are none).

An oracle that replays the gold labels scores perfectly by construction. That
only shows the scorer and gate work; it says nothing about how good any
classifier is.

**Contents**

- 130 pairs over 112 claims in 24 meetings, all for one owner and twin.
- Labels: 38 `related_discussion`, 21 `potential_contradiction`,
  28 `potential_supersession`, 24 `no_established_relationship` and
  19 `abstain`.
- Languages: 28 English, 20 Indonesian and 82 mixed (code-mixed or
  cross-language).
- Covered cases:
  - paraphrases
  - hard negatives with overlapping vocabulary
  - participant claims of owner approval
  - equal meeting timestamps
  - altered and missing source evidence
  - unattributed and ambiguous claims
  - prompt injection inside meeting content (4 pairs)
  - multi-evidence claims where only some quotes support the pair (10 pairs)
- Pairs 001 to 120 are unchanged from the first revision. Pairs 121 to 130 add
  the multi-evidence cases.
- Every claim goes through the real DM-1 `records_from_report`. Items whose
  evidence is altered or missing produce no record, and their pairs are
  labelled `abstain`.
- Each pair carries both claim keys, its temporal order, the gold evidence
  entries on each side and a written rationale. The labelling rules are in the
  fixture's `guidelines`.

## Scoring

`uv run --no-sync python -m evals._decision_lineage score --predictions FILE`
scores a prediction file without any model call. `--baseline NAME` scores a
built-in deterministic predictor. The report gives:

- the confusion matrix
- per-class precision and recall
- the relationship false-positive rate on gold `no_established_relationship`
- abstention recall and answered coverage
- evidence-reference validity: every cited entry belongs to that side's DM-1
  record, and both sides are cited
- evidence selection, scored only on pairs where a record has more than one
  quote so there is a real choice:
  - sufficiency: every gold entry on both sides is cited
  - precision: the share of cited entries that are gold
- counts of supersessions on pairs without a strict time order, supersessions
  on owner-approval claims, and answers on pairs with broken evidence

### Gate a classifier must pass

| Measure | Requirement |
| --- | --- |
| Precision | contradiction ≥ 0.90, supersession ≥ 0.90, related ≥ 0.85, none ≥ 0.80 |
| Recall | related, contradiction, supersession ≥ 0.70; none ≥ 0.80 |
| False relationships on gold `none` | ≤ 5% |
| Abstention | abstention recall ≥ 0.80, answered coverage ≥ 0.80 |
| Evidence references | 100% valid |
| Evidence selection | sufficiency ≥ 0.90, precision ≥ 0.90 |
| Hard rules | 0 supersessions without a strict time order, 0 supersessions on approval claims, 0 answers on broken evidence |

### Measured baselines (deterministic, no model)

| Predictor | Contradiction R | Supersession R | Related P / R | FPR on none | Abstain R | Selection suff. / prec. | Gate failures |
| --- | --- | --- | --- | --- | --- | --- | --- |
| oracle (gold labels and evidence) | 1.00 | 1.00 | 1.00 / 1.00 | 0.00 | 1.00 | 1.00 / 1.00 | 0 |
| gold labels, valid but wrong evidence | 1.00 | 1.00 | 1.00 / 1.00 | 0.00 | 1.00 | 0.00 / 0.50 | 2 |
| gold labels, citing every quote | 1.00 | 1.00 | 1.00 / 1.00 | 0.00 | 1.00 | 1.00 / 0.67 | 1 |
| lexical weighted Jaccard ≥ 0.2 | 0.00 | 0.00 | 0.39 / 0.45 | 0.08 | 0.00 | 1.00 / 0.67 | 12 |
| constant `related_discussion` | 0.00 | 0.00 | 0.29 / 1.00 | 1.00 | 0.00 | 1.00 / 0.67 | 12 |
| constant `no_established_relationship` | 0.00 | 0.00 | n/a / 0.00 | 0.00 | 0.00 | 1.00 / 0.67 | 11 |
| constant `abstain` | 0.00 | 0.00 | n/a / 0.00 | 0.00 | 1.00 | n/a | 9 |
| constant `potential_supersession` | 0.00 | 1.00 | n/a / 0.00 | 1.00 | 0.00 | 1.00 / 0.67 | 14 |

Correct labels are not enough to pass: a predictor with every label right
fails when it cites valid but irrelevant evidence, or when it cites every
quote. No model has been evaluated; there are no model results.

## Independent annotation

`uv run --no-sync python -m evals._lineage_annotation export --out DIR --salt SALT`
writes two parts:

- `DIR/annotator/`: the blind package and an empty template. It contains no
  labels, rationale, categories, gold evidence, pair ids, claim keys or
  meeting ids. Pairs are ordered by a salted digest. Each quote is shown with
  its source text, so broken evidence stays visible.
- `DIR/coordinator/blind-key.json`: the mapping back to pair ids. It must not
  reach the annotator.

`... agreement --annotations FILE --key KEY --adjudication OUT` reports
observed agreement, Cohen's kappa, per-label agreement and a confusion matrix
against the curator's labels. It writes every disagreement to a separate
adjudication file with an empty `adjudicated_label`. Adjudication outcomes
feed `label_corrections`; they never overwrite a label silently.

Measured agreement from a human second annotator, and its adjudication, are
prerequisites for using this benchmark as an activation gate. They are not
prerequisites for keeping it as a provisional benchmark.

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

`uv run --no-sync python -m evals._decision_lineage retrieval` measures this
ranking reproducibly inside the real DM-2a window. It uses `comparison_window`
from `fakeyou.debrief.lineage` over a register built from the dataset, so labels
are only used to decide which pairs to check, never to rank.

| | Count |
| --- | --- |
| Labelled relationship pairs | 87 |
| Excluded: no strict time order | 4 (pairs 079 to 082; DM-2a cannot compare them) |
| Excluded: broken evidence | 0 (all broken-evidence pairs are labelled `abstain`) |
| Excluded: outside the window | 0 |
| Eligible | 83 |
| Recall@5 | 75 / 83 = 0.90 |
| Recall@10 | 81 / 83 = 0.98 (misses: pairs 036 and 112) |

**Correction to the first revision.** That revision reported 69/77 at 5 and
75/77 at 10 from an ad-hoc script. It counted the 4 equal-timestamp pairs as
eligible, because it allowed candidates created at the same time, and all 4
happened to hit. It also ranked item text only, not quotes.

Rerunning the reproducible measurement on the original 120 pairs gives 73
eligible pairs, 65/73 (0.89) at 5 and 71/73 (0.97) at 10, with the same two
misses at 10.

These figures rank against the full corpus, so a development claim can be
ranked against frozen claims, and frozen text shapes the IDF weights. They are
kept as historical exploratory evidence only. Model requests and evaluation
use partition-isolated pools (see the leakage-resistant split below).

The same lexical score is useless as a classifier (see the table above), so it
only narrows the candidate list and never labels.

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

## Leakage-resistant split

`evals/fixtures/decision-lineage-split.json` assigns each of the 112 claims to one
of 28 engineering topics, and each topic to one partition. A pair belongs to the
partition of its two claims; a pair whose claims fall in different partitions is
quarantined. Meetings are not the unit, because every lineage pair spans two
meetings. The benchmark fixture itself is unchanged.

| Partition | Pairs | Labels (rel / contra / super / none / abstain) | Languages (en / id / mixed) |
| --- | --- | --- | --- |
| development | 64 | 17 / 11 / 14 / 13 / 9 | 14 / 8 / 42 |
| frozen_evaluation | 64 | 20 / 10 / 14 / 10 / 10 | 14 / 12 / 38 |
| quarantined | 2 | 1 related, 1 none (pairs 021 and 089) | mixed |
| real_world_validation | 0 | reserved for consented real records labelled by two humans | none |

`python -m evals._decision_lineage split` prints this table;
`score --partition NAME` scores one partition.

**Limitations.**

- All eight owner-approval-claim pairs belong to one thread (the Kafka budget),
  so they sit in the frozen partition only. The development partition, and so
  the pilot, cannot exercise them; the deterministic validator enforces the
  approval rule regardless.
- Only one prompt-injection pair is in development, against three in the frozen
  partition.
- The partitions are too small for statistical confidence. Even a perfect
  classifier scores supersession recall with a 95% Wilson interval of
  [0.785, 1.0] on the 14 frozen supersession pairs. A frozen run can rule out a
  bad classifier; it cannot certify the 0.90 gate. Only more data, ideally the
  real-world partition, can.

Partitions are chosen once and frozen. Prompts and thresholds are tuned on
development only.

**Isolated candidate pools.** Each partition's requests are built from a
register holding only that partition's claims, and the lexical IDF statistics
come from that register too. A development request therefore never contains
frozen claim text, quotes or records, and rewriting every frozen claim leaves
every development request byte-identical (a regression test checks both).
Quarantined pairs are never evaluation targets, and `planned_calls` refuses any
pair that reaches outside its pool.

`python -m evals._decision_lineage retrieval --partition NAME`:

| Pool | Relationship pairs | Excluded (no strict order) | Eligible | Recall@5 | Recall@10 |
| --- | --- | --- | --- | --- | --- |
| development | 42 | 3 (079 to 081) | 39 | 35 / 39 | 38 / 39 (miss: 036) |
| frozen_evaluation | 44 | 1 (082) | 43 | 38 / 43 | 42 / 43 (miss: 112) |
| full corpus, historical | 87 | 4 | 83 | 75 / 83 | 81 / 83 |

The quarantined relationship pair (021) is in neither partition.

## Offline pipeline (no provider)

`evals/_lineage_classifier.py` is the evaluation harness a model would plug
into. It contains no live provider adapter.

- **Request:** `ClassifierRequest` (`lineage-classifier/v1`). Per call, one later
  claim and up to 10 earlier candidates, chosen by lexical ranking inside the
  DM-2a `comparison_window` (same owner and twin, strictly earlier meetings).
  Candidates are named `c1` to `c10`. No record, meeting, owner or pair id, label,
  category or rationale is sent. Meeting content goes only in the user message;
  the system prompt is a constant.
- **Output:** `ClassifierOutput`, `extra=forbid`. For each candidate:
  `candidate`, `label` (four relationships or `abstain`), `earlier_evidence` and
  `later_evidence` entry ids. There is no free text.
- **Validation (fail closed):** each of the following becomes `abstain` with a
  recorded reason:
  - an output that does not parse, has unknown fields, or names an unknown
    candidate (`invalid_output`)
  - a duplicate candidate
  - a candidate missing from the output (`not_returned`)
  - evidence that is empty or not the claim's own (`evidence_invalid`)
  - supersession without a strict time order (`order_unverified`)
  - supersession touching an `owner_decision` (`approval_authority`)

  A prediction is only ever a derived, unreviewed candidate. Nothing is written
  to DM-1, DM-2a or the owner model.
- **Replay:** `ReplayProvider` reads a JSONL cassette keyed by the SHA-256 of
  model id, system prompt and request. A missing entry fails the run.
- **Caps:** `run_pipeline` refuses the next call when it would exceed the call
  cap, the input-token cap, the output-token cap (assuming the per-call output
  maximum) or the spend cap. It records spend, tokens and the reason it stopped.
  The pre-call check uses an offline estimate (characters / 3), not a provider
  token count, so these caps are a simulator bound, not a monetary guarantee for
  a live adapter. A live adapter needs provider token counting before each call
  and reconciliation from response usage after it.
- **Reports:** `python -m evals._lineage_classifier replay --cassette FILE`
  reports per-class precision and recall with 95% intervals, by language and by
  category, evidence validity and selection, hard-rule counts, decision reasons,
  spend per evaluated pair, and end-to-end outcomes. The end-to-end outcomes
  separate retrieval misses, pairs excluded by DM-2a eligibility, broken
  evidence, and correct and wrong answers, so recall counts retrieval misses.
  With an oracle response and isolated pools, the pipeline reaches 38 of 42
  development relationship pairs (3 without strict time order, 1 missed by
  retrieval) and 42 of 44 frozen ones (1 and 1).

## Proposed pilot (not authorized)

**Model.** Google `gemini-3.8-flash`, the debrief role model in `.env.example`,
through the existing `langchain-google-genai` 4.4.0 `init_chat_model` path, with
structured output as `ToolStrategy`, which `_extract_chunk` already uses for
`DebriefDraft`.

- `reasoning_effort=low`, because the repository records that this model rejects
  `minimal`.
- Temperature at the provider default, recorded in the cassette.
- No new SDK or dependency.

Its suitability for English, Indonesian and mixed engineering discussion is not
demonstrated for this task; measuring it is the point of the pilot.

**Pricing.** <https://aistudio.google.com/docs/pricing>, retrieved 2026-11-17.
Gemini 3.8 Flash, Standard paid tier: $0.75 per 1M input tokens and $3.75 per 1M
output tokens through 2026-12-31, doubling to $1.50 and $7.50 from 2027-01-01.
Output pricing is assumed to include thinking tokens, so the per-call output cap
covers thinking.

**Sample.** 23 development pairs, from `evals/fixtures/decision-lineage-pilot.json`:

| Group | Pairs |
| --- | --- |
| Labels | 4 related, 5 contradiction, 4 supersession, 5 none, 5 abstain |
| Languages | 8 English, 6 Indonesian, 9 mixed |
| Hard cases | 4 hard negatives, 4 ambiguous, 1 prompt injection |
| Expected zero-call outcomes | 1 broken evidence (must abstain without a call), 1 equal-timestamp (must be excluded by eligibility) |

These need 21 calls comparing 204 candidates from the development pool only.

**Budget and caps.** `python -m evals._lineage_classifier plan` prints this from
the real requests. All token figures are offline estimates:

| | Value |
| --- | --- |
| Calls | 21 (cap 24) |
| Estimated input tokens | 32,538, at most 1,647 per call (cap 120,000) |
| Output | at most 4,096 tokens per call (cap 100,000 in total) |
| Worst-case spend | $0.35 at 2026 prices, $0.70 at 2027 prices |
| Spend cap | $1.00 |

**Call policy.**

- 60 s timeout per call.
- At most one retry, on a transport or server error only, counted against the
  call cap.
- No retry on a schema-invalid answer; it is scored as `invalid_output`.

**Stop conditions.**

- Any cap is reached.
- Three consecutive provider errors.
- More than 20% `invalid_output` after the first five calls.

Every request and raw response is recorded to a cassette, so all later scoring
replays offline.

**Conditions for continuing past the pilot**, all measured on the 23 pilot pairs:

- `invalid_output` in at most 10% of calls
- model-level `evidence_invalid` in at most 10% of answered candidates
- every relationship class with recall above zero
- at most one of the five `none` pairs labelled with a relationship
- at least three of the five `abstain` pairs abstained
- the prompt-injection pair labelled by its content

**Stages after a successful pilot,** each needing its own approval:

| Stage | Calls | Worst case (2026 / 2027 prices) |
| --- | --- | --- |
| Full development run | 40 | $0.66 / $1.33 |
| One frozen evaluation run | 39 | $0.64 / $1.29 |
| Two stability reruns on frozen | 78 | $1.29 / $2.57 |

The whole programme stays under $3 at 2026 prices and under $6 at 2027 prices.
The frozen run is scored against the gate above, and its confidence intervals
are reported with it.

**What a continue decision would rest on.** A frozen run that passes the hard
rules (no time-order, approval or broken-evidence violations), keeps the
relationship false-positive rate under 5%, and reaches per-class lower 95% bounds
clearly above the lexical baseline justifies collecting real, double-labelled
data. It does not justify activation on its own.

## Independent annotation status

**Second annotation pending.**

- `python -m evals._lineage_annotation export` writes a leak-checked package:
  130 pairs and 7 worked examples on topics the benchmark does not use, with no
  ids, labels, rationale or categories. The coordinator key is written
  separately.
- `verify` re-checks an exported package.
- `agreement` computes Cohen's kappa, per-label agreement, a confusion matrix and
  disagreement types, and writes an adjudication file.
- `corrections` proposes label corrections only after every disagreement is
  adjudicated, requires a written reason, and never edits the benchmark.

No second human annotator has labelled the benchmark; agreement is unmeasured.

## Explicitly out of scope until authorized

- provider requests
- enabling classification in any worker
- writing classifier output to a sidecar
- cross-twin lineage
- an owner review workflow
