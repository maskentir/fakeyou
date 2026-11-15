# Offline support matcher evaluation — Phase 5A Hardening

Parent: PR #214, `1ada18c07ef40f08c1ba51b7a13c9d8f9ddd1759`.
Contract: PR #205, section 13 and locked decision 15. New provider requests: 0;
new provider spend: US$0. PublicationContext remains off.

## Inputs and method

Ten existing look responses, read-only: eight visual-inspection responses and
one camera and one screen simulator response. The new labels file records the
SHA-256 of each input and 41 independently stated claim proposals, including
wrong values, entities, attributes, and unproduced looks. Labels describe what
the recorded response supports; they are not independent verification of the
image or of the provider's accuracy. No historical input was edited.

The `answer` run measures the evidence available to the runtime matcher today.
The `answer_and_observed_facts` run is a separate diagnostic hypothesis; it does
not change the runtime evidence or authorize broader support. The renderer uses
a placeholder evidence binding to measure field/pattern availability only.
Neither run measures freshness, source identity, publication, or audibility.

False support = accepted false claims / false labels. False rejection = true
labels that cannot be supported and rendered / true labels. Coverage = true
labels that can be supported and rendered / true labels. Abstention = all
unrenderable or unsupported proposals / all proposals. A successful rendering
here is a candidate statement, not PCM or a playback receipt.

## Results

| Input / language | Claims (true / false) | False support | False rejection | Coverage | Abstention |
| --- | --- | --- | --- | --- | --- |
| Cassette answer / EN | 41 (19 / 22) | 0/22, 0% | 12/19, 63.16% | 7/19, 36.84% | 34/41, 82.93% |
| Cassette answer + facts / EN, diagnostic | 41 (19 / 22) | 0/22, 0% | 8/19, 42.11% | 11/19, 57.89% | 30/41, 73.17% |
| Cassette / ID | 0 | not measured | not measured | not measured | not measured |
| Synthetic / EN | 38 (11 / 27) | 0/27, 0% | 1/11, 9.09% | 10/11, 90.91% | 28/38, 73.68% |
| Synthetic / ID | 26 (8 / 18) | 0/18, 0% | 1/8, 12.50% | 7/8, 87.50% | 19/26, 73.08% |

| Input / language | Missing evidence | Ambiguous relation | Unsupported shape/field | Other unsupported |
| --- | --- | --- | --- | --- |
| Cassette answer / EN | 5 | 0 | 0 | 29 |
| Cassette answer + facts / EN | 5 | 0 | 4 | 21 |
| Synthetic / EN | 3 | 5 | 1 | 19 |
| Synthetic / ID | 1 | 4 | 1 | 13 |

Exact reasons and denominators: `evals/fixtures/support-matcher/results.json`.

## Reproduction and limits

Run `.venv/bin/pytest evals/test_support_matcher_eval.py -q` (9 tests).
The tests reproduce the result from labels and recorded responses, verify input
hashes, reject false labels, and prohibit socket connections during evaluation.
No provider is needed. Synthetic cases are read from the existing
`tests/fixtures/claim-support-cases.json` without edits.

Zero false support is a result on these labels, not proof of safety in general.
Production-like English coverage is low: short provider answers often omit the
entity and attribute relation needed by the matcher. Indonesian cassette
coverage remains unmeasured; synthetic Indonesian results do not fill that gap.
No additional dataset, provider request, or activation is authorized by this
report. The existing matcher/renderer rules remain unchanged.
