# Offline support matcher — revision 2

Tested implementation: `be09303924995fd28ba5b69dda5405a5ab70426e`. Provider requests 0; US$0.
Historical labels/cassettes/results were read without edits; numeric results
are unchanged. Input and previous-result hashes are recorded in the JSON.

| Input / language | False support | False rejection | Coverage | Abstention |
| --- | --- | --- | --- | --- |
| Cassette answer / EN | 0/22 | 12/19, 63.16% | 7/19, 36.84% | 34/41, 82.93% |
| Cassette answer + observed facts / EN, diagnostic only | 0/22 | 8/19, 42.11% | 11/19, 57.89% | 30/41, 73.17% |
| Cassette / ID | Not measured | Not measured | No samples | Not measured |
| Synthetic / EN | 0/27 | 1/11, 9.09% | 10/11, 90.91% | 28/38, 73.68% |
| Synthetic / ID | 0/18 | 1/8, 12.50% | 7/8, 87.50% | 19/26, 73.08% |

**Product liveness remains an acceptance blocker.** Correct-claim coverage is
36.84% under the runtime answer-only evidence. Short answers often omit the
entity/attribute relation L1 requires. This review correction does not relax L1,
promote observed facts into runtime evidence, infer missing relations from the
question, alter labels, or make another provider request. Such a change would
need its own reviewed evidence contract.

Zero false support describes only this labelled set. Labels are derived from
recorded look responses, not independent frame truth. Placeholder rendering
measures statement availability, not actual output, freshness or audibility.
Synthetic ID measurements are not Indonesian cassette evidence. These figures
are not production readiness or compliance claims.

Reproduce with `.venv/bin/pytest evals/test_support_matcher_eval.py -q` and
`from evals._support_matcher import evaluate; evaluate()`. Full denominators and
abstention categories are in `2026-11-15-support-matcher-offline-r2.json`.
