# Proposed amendment: local preparation and final PCM publication

Status: **DRAFT FOR CONTRACT REVIEW — NOT APPROVED, NOT IMPLEMENTED**.

This is a separate proposal against the binding contract in
`2026-11-15-publication-safety-liveness-contract.md` (#205). That file remains
unchanged and authoritative. This draft does not resolve NC-76 in the current
audit and does not authorize a runtime reinterpretation, activation, mark-ready,
merge, provider experiment, or a request of any kind. PublicationContext stays
OFF; Phase 5B NO-GO; new provider requests 0; new provider spend US$0.

## Problem and scope

The r2 diagnostic (`evals._publication_conflict.probe`) reproduces local current
synthesis before a valid preliminary gate and an invalid final commit, with
zero current PCM. Runtime model text remains excluded, but original NC-76 still
fails. I7 defines the final commit as evaluation and first-frame handoff in one
non-suspending section. Dynamic synthesis must make that first PCM available.
Demanding that completed final commit precede synthesis creates a cycle in the
current asynchronous pipeline:

`final commit → synthesis → first PCM → final commit`.

Calling an earlier preparation approval a final commit does not solve it. A
300 ms synthesis after such an approval also exceeds V1's 250 ms output bound;
re-evaluation afterward becomes the actual final commit again.

The proposed scope is **local preparation authorization only**. It does not
relax evidence matching, claim grammar, page/source/subject identity, output
freshness, receipt accounting, consent, provider budgets, or participant
measurement. No physical provider request becomes permissible by this amendment.

## Options for review

| Option | Behavior | Latency / liveness | Privacy and cost | Assessment |
| --- | --- | --- | --- | --- |
| A — separate local preparation from publication | After deterministic claim verification, an explicitly authorized local synthesizer may prepare runtime pattern audio before final publication validation. Only the final PCM commit can authorize writing. | Preparation waits do not consume the later 250 ms window. Work may be discarded after scene change or cancellation. This improves feasibility, not measured product coverage. | Prepared text and PCM may contain sensitive screen facts in process memory. No provider egress is permitted. Local CPU/memory work still needs limits and retention controls. | Recommended for review; requires an explicit NC-76 amendment. |
| B — retain strict NC-76 | Dynamic current text cannot enter synthesis before final commit. Current speech uses only audio that already lawfully exists and matches the approved rendering, with all publication gates still applied. | Arbitrary numbers/names/dates generally have no pre-existing audio. Dynamic current speech remains unavailable. A ready catalog does not supply arbitrary historical/current speech. | No new local speculative audio; existing audio still needs lawful use and retention controls. Catalog prebuilding is synthesis and creates no provider authorization. | Valid strict alternative if the liveness restriction is accepted. |
| C — commit before asynchronous synthesis, then write | Treat preparation approval as final publication approval. | A wait separates validation from first PCM and can exceed 250 ms; later revalidation changes which decision is final. | Would still need synthesis authorization and could leak or spend on unused output. | Rejected: contradicts I7/V1 and does not fix bounded output. |

No option establishes a bounded physical writer. Option A is not a workaround
for V6. Provider synthesis remains unavailable until Y2 has an independently
reviewed physical-request authorization and budget ledger.

## Proposed contract wording — option A only

The text below is a reviewable amendment, not the contract in force. Do not
apply it to #205 or runtime behavior until separately approved.

**Proposed replacement NC-76:**

> In a visual turn, only an approved runtime rendering whose claim and evidence
> pass deterministic support and field checks may enter an explicitly authorized
> local preparation path before final publication commit. A preparation grant is
> not a publication decision. A current attempt whose final publication decision
> is not `valid` hands no PCM to the writer, contributes zero current PCM, is not
> counted as spoken, and does not enter the meeting record as twin speech.
> Provider requests require their own Y2 authorization and physical-attempt ledger;
> this exception authorizes no provider request. Raw model-written text never
> reaches a synthesizer on any path.

**Proposed clarification beside I7:**

> Final publication commit retains its existing definition: the synchronous
> final evaluation of the immutable binding and handoff of the first already
> available PCM frame, without suspension between them. Local preparation and
> any asynchronous synthesis, readiness, queue or back-pressure wait precede this
> section. Preparation grants and preliminary decisions are never recorded as
> committed publication decisions. Receipt evidence remains necessary for local
> audio; nothing here proves participant audibility.

I1–I15 otherwise remain in force. In particular, I13/I14, V1–V6 and Y1–Y6 are
not weakened. The accepted 5 s freshness, 250 ms validation/output allowance,
and 50 ms uncertainty are unchanged. The existing H1–H6 historical gate remains
separate from synthesis availability.

## Preparation gate proposed for option A

Every condition below must hold before the local synthesizer is entered:

1. The turn has been admitted and classified. No model sentence is prepared
   speculatively before its visual/nonvisual classification is known.
2. The selected claim has an immutable evidence item with a produced look and
   deterministic support. Model declarations of scope or support grant nothing.
3. The runtime alone creates an approved EN/ID pattern and verifies G1–G8.
   Unsupported, ambiguous, negative or undeclared claims do not enter preparation.
4. The local synthesizer is explicitly wired and authorized for this purpose.
   A mode string or a claimed local flag on a remote/fallback adapter is not
   sufficient. Any route that can make a provider request must fail closed.
5. At most one local preparation is in flight per turn. The artifact records a
   preparation ID, text digest, pattern/version/language, original evidence IDs,
   page/source/subject identity, authorization and outcome. These records do not
   assert publication validity or local PCM output.
6. Prepared audio is transient and is not persisted or externally uploaded by
   default. Discard it on cancellation, rejection, replacement or turn completion.
   Python buffer release does not promise cryptographic erasure of process memory.
7. Every later retry rechecks authorization. A retry or fallback cannot silently
   switch to a remote synthesizer. Unavailable local synthesis yields catalog
   audio only if separately available and authorized, otherwise journaled silence.

Prepared PCM is an untrusted candidate for use, not evidence of the claim or of
freshness. Every publication attempt has its own binding, publication ID and
final decision even if prepared bytes are reused. Reusing an artifact cannot
refresh the original analyzed capture, change its claim, or transplant approval
from another page, source, subject, pattern or language.

## Publication and physical-output gates retained

After preparation and every asynchronous wait, evaluate against the latest
accepted perception state. A stale, changed, unmapped or ambiguous statement
hands nothing to the writer. No await belongs between final evaluation and the
first-frame handoff. Later evidence does not rewrite a committed decision.

Writer acceptance and successful write timestamps come from writer observations
on a proven clock. The first successful write must meet both the 250 ms allowance
and sample freshness at that write. A writer that cannot prevent output beyond
the permitted bound, or safely establish the approved revalidation branch, still
blocks activation under V6. Honest late-write telemetry is not such a guarantee.

Cancellation must not convert an unobserved executor write into an assertion of
zero output. The production blocking-write/cancellation problem remains open;
local fake writer tests do not resolve it.

## Proposed negative controls — not executed acceptance claims

These are additions to be implemented only after contract approval. The original
NC-01–NC-119 numbering is not changed by this draft.

| ID | Deterministic scenario | Required result under option A |
| --- | --- | --- |
| NCA-01 | Supported rendered current statement; local preparation takes 300 ms; latest exact sample remains fresh | Preparation may complete; a separate final evaluation occurs afterward. The 250 ms output window starts there, never at preparation approval. |
| NCA-02 | Scene changes during local preparation | Final decision prevents current PCM; discarded preparation is not counted as speech. |
| NCA-03 | Sample expires during writer readiness/back-pressure after preparation | Evaluation after the wait prevents write. |
| NCA-04 | Preliminary gate valid, non-exact sample before final commit | No current handoff, including when prepared audio exists. |
| NCA-05 | Local authorization absent or revoked before preparation/retry | No synthesizer entry; no remote fallback; journaled silence/catalog under its own gate. |
| NCA-06 | Raw model text through reply, direct, cue, speculative/lookahead, retry or recovery path | No raw model sentence reaches a synthesizer. |
| NCA-07 | Unsupported claim or field not in an approved evidence span | No preparation; correct catalog selection or silence. |
| NCA-08 | Artifact from another page epoch/session, stream generation or participant | Artifact cannot carry publication approval; refuse reuse that changes the bound evidence/identity. |
| NCA-09 | Same text, different evidence or publication attempt | Distinct binding and final decision; receipt and counting never transfer from the earlier attempt. |
| NCA-10 | Prepared model audio predates visual classification | Discard it; no PCM write and no twin speech. |
| NCA-11 | Barge-in/stop during preparation or before final commit | No current handoff; terminate/discard work with a recorded preparation outcome. |
| NCA-12 | Output delayed beyond allowance and writer can withhold | Zero PCM, `write_delayed_past_validation`, unspoken receipt. |
| NCA-13 | Blocking fake writes beyond allowance despite permission | Truthful audio receipt, freshness unproven, no compliant/fresh verdict; activation gate remains failed. |
| NCA-14 | Acceptance/write instant missing or on another clock | No proof of fresh current publication. |
| NCA-15 | Prepared audio exists, no successful write | No spoken count or meeting record entry. |
| NCA-16 | Local preparation adapter attempts an external request or remote fallback | Local authorization does not cover it; transport entry is denied before request. |
| NCA-17 | Valid historical fallback text, no authorized synthesis/audio path | Silence with reason; ready text alone grants nothing. |
| NCA-18 | Provider mode requested with no physical-attempt budget ledger | Fail closed. |
| NCA-19 | Prepared audio contains a sensitive value and is abandoned | No automatic persistence/upload; release transient artifact without claiming secure memory erasure. |
| NCA-20 | No visual needs or opt-in publication context absent | Preserve existing behavior; no global activation or implicit provider authorization. |

## Decision requested in review

Review option A's explicit local-only exception, artifact/retention limits and
replacement wording, or select the strict option B. No implementation or contract
edit follows from publication of this draft. Approval of a design amendment would
still not authorize activation, external provider experiments, V6/Y2 exceptions,
or product acceptance.

Product liveness work is a separate proposed evaluation in
`2026-11-15-visual-claims-product-evaluation-plan.md`. Schema/adapter proposal
availability and EN/ID evidence quality require their own measurements. The
existing 7/19 evaluator coverage is not end-to-end model-flow coverage.
