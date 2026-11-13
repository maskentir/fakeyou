# Phase 3b-A: offline verdict contract and evaluation harness

Phase 4 was accepted and PR #192 merged as `62875d84553deec9db39a5a42168b6475ced7d79`.
This work follows the approved sequence: offline contract and independent evaluation first,
provider integration second, live end-to-end measurement last.

## Contract

`src/fakeyou/perception/inspector.py` defines an unwired `VisualInspection` value:

- `verdict`: `supported`, `not_visible`, or `unclear`.
- `answer`: a nonempty string for `supported`; null for either abstention.
- `observed_facts`: nonempty strings representing the model's assertions, not verified facts.

`supported` means the model claims that the image supports its answer.
`not_visible` means the requested information is absent from the supplied view.
`unclear` means a relevant region is present but its content cannot be established.
These verdicts do not replace acquisition/status telemetry and do not certify truth.
Unknown fields, unknown verdicts, empty claims, non-string answers, and answer/verdict
contradictions fail validation.

## Independent labels and dataset

The eight cases use three controlled PNG images: a readable slide, the slide with its date
covered, and a schematic person with a blue shirt and red mug. The source generator is
`evals/_visual_images.py`. This is a synthetic contract dataset, not a representative sample
of real camera images. It contains five answerable cases, two absent-information cases,
and one obscured-information case.

`evals/fixtures/visual-inspection/cases.json` supplies expected verdicts, permitted answer
values, bounding boxes in pixels, visible/absent/obscured annotations, allowed fact strings,
and reasons derived from the controlled scenes. Labels are authored independently of
evaluated outputs. They are not represented as human-reviewed natural-image labels.
Each image has a SHA-256 digest; stale hashes, duplicate case IDs, invalid answer labels,
and bounding boxes outside images fail loading. The generator writes images only; it
does not infer or rewrite evaluation labels from predictions.

## Determinism contract for the controlled images

Two invariants hold the controlled images, and they are deliberately separate.

- Fixture identity: every image the dataset labels is pinned by the SHA-256 digest
  recorded in `cases.json`. The committed bytes are the approved artifact; a replaced
  or regenerated image fails loading until that digest is updated deliberately.
- Generator correctness: `evals/_visual_images.py` must draw the same picture as the
  committed images, compared as decoded pixels rather than encoded files.

PNG bytes are not a portable artifact. The macOS Pillow wheel bundles zlib-ng and the
manylinux wheel bundles zlib, so identical pixels compress into different IDAT streams
and one generator writes different files on different machines. Pixel equality is the
meaningful contract; byte equality is not. The drawing itself is stable: the pinned
Pillow build rasterizes the bundled font identically on both platforms.

The fake inspector receives only image bytes and the question. It cannot access expected
verdicts, expected answers, annotations, or reasons through the evaluation interface.
`fake-predictions.json` is a separate scripted response fixture. Its successful score tests
the harness plumbing; it provides no evidence about an actual model's vision quality.

## Deterministic scoring

The report preserves case-level outcomes and the count and denominator for each rate.
Normalization applies Unicode NFKC, case folding, whitespace folding, and removal of
punctuation at the string edges. No keyword routing, substring matching, semantic judge,
random sampling, or model self-assessment determines truth.

| Metric | Numerator | Denominator |
| --- | --- | --- |
| Verdict accuracy | Exact agreement with independent verdict | All cases |
| Answer accuracy | Answer matches a permitted answer value | All answerable cases |
| Grounding precision | Answers and observed facts supported by the label rubric | All assertions in valid outputs |
| False claim rate | Valid outputs with any assertion outside the label rubric | Valid outputs making at least one assertion |
| Abstention precision | Abstentions on independently unanswerable cases | All valid abstentions |
| Abstention recall | Abstentions on independently unanswerable cases | All unanswerable cases |
| False abstention rate | Abstentions on answerable cases | All answerable cases |
| Invalid output rate | Responses failing JSON/schema validation | All cases |
| Model error rate | RuntimeError or TimeoutError from the inspector | All cases |

A zero denominator produces null. Invalid responses and model errors remain in case-level
results and accuracy/recall denominators, and prevent a passing report. They are not
interpreted as safe abstentions or zero false claims. An always-abstaining model fails
answer accuracy; an always-supported model with invented answers fails independent
grounding. A correct answer with an invented observed fact also fails.

Answer relevance and factual support are separate: an irrelevant answer containing a
visible annotated fact can have grounded assertions and still fail answer accuracy.
Abstention recall accepts either abstention subtype on unanswerable cases; exact verdict
accuracy separately checks the distinction between absent and obscured information.

The false claim metric is a closed-set rubric violation rate. An unlisted paraphrase can
fail even when a person would accept it. It is not a general measurement of real-world
hallucination. Provider integration needs reviewed aliases or independent adjudication
for previously unseen free-form assertions; the model's own verdict cannot supply that.

## Acquisition metrics and comparisons

The canonical report classifies the latest evidence attempt. `evidence_provenance` and
`evidence_cache` count activity across all resolved events, including earlier attempts.
They measure acquisition activity and cache use, not solely evidence used by the final
answer. Evaluation must retain this distinction when comparing with the archived metric
snapshot from `bb34875`. This harness does not reinterpret that archive or change its values.

## Execution and acceptance

1. Add failing schema tests in `tests/test_inspector.py`; implement the value contract.
2. Add failing independent-label and adversarial scoring tests in
   `evals/test_visual_inspection_eval.py`; implement the dataset loader and evaluator.
3. Verify strict parsing, wrong supported answers, unsupported added facts, irrelevant
   visible facts, always-abstaining outputs, malformed outputs, and model failures.
4. Run the offline suite, Ruff, mypy, import contracts, and dependency checks.

Run the focused check with:

```sh
uv run --no-sync pytest tests/test_inspector.py evals/test_visual_inspection_eval.py -q
```

Regenerate images with `uv run --no-sync python -m evals._visual_images`. Regeneration is
for inspection, not for replacement: another zlib build rewrites the committed files
without changing the picture, so keep the committed bytes and their digests unless the
picture itself changed. A changed picture requires inspecting the images and reviewing
the independent labels before updating digests.

Phase 3b-B remains separate: actual vision calls, prompt/schema integration, observed
provider parsing errors, and cassettes recorded with valid provider credentials. The
evaluator classifies `RuntimeError` and `TimeoutError` as `model_error`; the runtime
`ProviderFault` is neither, so the provider adapter must turn a genuine provider fault
into a case-level error instead of failing the whole evaluation, while programming
errors stay visible as harness failures. Phase 5 measures live groundedness/abstention,
latency p50/p95, vision calls per turn, and meeting cost. This phase changes no
production prompt, runtime acquisition path, or cassette.
