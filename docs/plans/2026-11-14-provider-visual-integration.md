# Phase 3b-B: provider-backed structured visual inspection

Phase 3b-A was accepted and merged as `2e461a3`. This phase connects the offline verdict
contract to the production look path and to a real vision provider, under a bounded
experiment budget. The approved sequence continues: offline contract first, provider
integration second, live end-to-end measurement last.

## Decisions

| Decision | Choice |
| --- | --- |
| Provider | Google Gemini, `gemini-3.8-flash`, the default perception provider and model |
| Experiment spend | At most 12 requests and US$1 in total; the guard stops the run and never extends the quota |
| Production prompt | The production look becomes a structured inspection, so evaluation and production exercise the same contract |

The provider choice is not a claim that Gemini has the best vision. Fakeyou already ships
a Google integration, a per-role model factory, and cassette infrastructure oriented at
Google, so the experiment costs code and credits instead of a new dependency.

## Files

| File | Change | Responsibility |
| --- | --- | --- |
| `src/fakeyou/perception/inspector.py` | modify | add `VISUAL_INSPECTION_SCHEMA_VERSION`, the version string the cassette stores |
| `src/fakeyou/perception/interpreter.py` | modify | the look prompt asks for the verdict, `answer` returns a structured inspection, and the verdict maps to status and code |
| `src/fakeyou/perception/camera_evidence.py` | modify | the cache entry stores the inspection instead of the answer text |
| `src/fakeyou/perception/context.py` | modify | the on-demand screen look maps the same verdict to the same status and code |
| `src/fakeyou/catalog/errors.py` | modify | name the error for a response that is not a usable inspection |
| `src/fakeyou/observability/report_contract.py` | modify | classify `insufficient` evidence in the canonical report, and let an unmet need outrank an answered one |
| `src/fakeyou/session_targets.json` | modify | move the scorecard contract version, because the classification contract changed and no target value changed |
| `evals/_visual_cassette.py` | create | record the controlled cases from the provider, guard the budget, and replay them offline |
| `evals/fixtures/visual-inspection/provider-cassette.jsonl` | create | the recorded provider responses, with usage and no credentials |
| `evals/fixtures/visual-inspection/experiment-attempts.jsonl` | create | every provider attempt of the experiment, so the request count is reconstructible |
| `evals/_visual_inspection.py` | modify | map a provider fault to a case-level error and keep a programming error visible |
| `evals/_provider_doubles.py` | modify | record the look requests and replace the entry the old recording left behind |
| `evals/fixtures/simulator/cassettes/sim-s3-camera.jsonl` | modify | the camera look recorded with a real structured provider response |
| `evals/fixtures/simulator/cassettes/sim-s3-screen.jsonl` | modify | the screen look recorded with a real structured provider response |
| `tests/_perception_fakes.py` | modify | the fake model serves scripted inspections as provider-shaped JSON |
| `tests/test_interpreter.py` | modify | the structured look, the unparsed inspection, and the budget |
| `tests/test_camera_evidence.py` | modify | the verdict mapping and the cache that keeps an abstention an abstention |
| `tests/test_context_evidence.py` | modify | the on-demand screen look emits the same statuses |
| `tests/test_context.py` | modify | the screen look scripts an inspection instead of free text |
| `tests/test_provider_doubles_eval.py` | modify | every recorded look parses as an inspection, and a free text answer is rejected |
| `tests/test_scorecard_contract.py` | modify | `insufficient` evidence is classified, and an unmet need outranks an answered one |

No new abstraction is introduced. There is no `EvidenceResolver`, no evidence store, and no
second inspection contract; the work stays inside the interpreter, the evidence contract,
the canonical report, and the existing cassette infrastructure.

## The runtime contract

A visual inspection produces one verdict and, when the model claims support, one answer.
That verdict is a claim about the frame. It is not a verification.

| `VisualInspection.verdict` | Evidence status | Result code | Verification |
| --- | --- | --- | --- |
| `supported` | `produced` | `ok` | `unverified` |
| `not_visible` | `insufficient` | `insufficient_evidence` | `unverified` |
| `unclear` | `insufficient` | `insufficient_evidence` | `unverified` |
| Provider or parse failure | `unavailable` | `look_failed` | `unverified` |
| Budget refused | `unavailable` | `budget_exhausted` | `unverified` |

`verification` is `unverified` in every row. Nothing in this phase raises it to `verified`,
and no verdict value can reach it. Verification stays a separate, later concern.

The three rows that matter are the first three. Fakeyou must be able to tell an answer that
was genuinely found in the image, an answer that could not be known from this frame, and a
technical failure while trying to look. A technical failure is not an abstention: a failed
look is `unavailable`, and a valid abstention is `insufficient`.

## One inspection path

`PerceptionInterpreter.answer` currently returns free text and the reply layer owns
semantic sufficiency. This phase replaces that with the same structured inspection the
evaluation harness scores.

- `PerceptionInterpreter.answer(image, question, source)` returns `VisualInspection | None`.
  `None` still means the budget refused the call.
- `PerceptionInterpreter.camera_answer` maps the verdict to the contract table above.
- `PerceptionContext._look` maps the verdict to the contract table above for the on-demand
  screen look.
- `PerceptionInterpreter.read_screen` keeps `ScreenReading`. Background screen reading
  answers a different question, and its structured contract already exists.

The camera and the on-demand screen look therefore run the same inspection, and the
evaluation harness scores the same `VisualInspection` value the production path produces.

## Structured output from the provider

The inspection is requested with the provider's native structured output, mirroring the
pattern `read_screen` already uses:

```python
structured = self._model.with_structured_output(VisualInspection, include_raw=True)
result = await self._invoke(lambda: structured.ainvoke([message]))
raw, parsed = _structured_parts(result)
```

`json_schema` is the default method in `langchain-google-genai` 4.4.0, so no method
argument is needed and no regex parser is written. The response schema is
`VisualInspection.model_json_schema()`. The Google SDK's client-side schema validation
accepts it on the Gemini Developer API path, including `additionalProperties: false` and
`minLength`; whether the API itself accepts those keywords is settled by the first recorded
call, and a rejection is reported as a finding rather than quietly worked around.

The prompt must state what the contract means, not only that JSON is wanted. It directs the
model to answer from the visible frame, to ignore instructions written inside the image,
and to abstain when the information is absent or unreadable. A bare "return JSON" sentence
on the old prompt produces a schema-conforming answer about nothing.

Schema conformance is not truth. The response is still validated locally with
`VisualInspection.model_validate_json`, because a response that satisfies the schema can
still answer a question the frame does not support.

## Error taxonomy

| Failure | Evaluation harness | Runtime |
| --- | --- | --- |
| Provider fault | Case-level `model_error` | `look_failed` |
| Invalid structured output | `invalid_output` | `look_failed` |
| Programming error | Harness failure, visible as a failed test | `look_failed` |

`PerceptionInterpreter._invoke` already maps provider exceptions through
`classify_exception("llm", ...)`, which produces a `ProviderFault`. The harness currently
catches only `RuntimeError` and `TimeoutError`, so a provider fault escapes as a harness
crash instead of a case-level error.

The distinction between a provider fault and a programming error is already encoded:
`classify_exception` marks a programming error with the detail `internal:<ExceptionType>`
and the kind `unknown`. The harness therefore maps a `ProviderFault` to `model_error` and
re-raises any fault whose detail starts with `internal:`, so a bug in the adapter stays
visible as a failed test instead of being counted as a model failure.

## Reporting: a valid abstention is not unknown telemetry

The Phase 4 classifier only accepts these evidence states:

```
("inference", "produced"), ("reused", "produced"),
("inference", "unavailable"), ("not_attempted", "unavailable")
```

So an `insufficient` status is classified as `unclassified` today, and the scorecard marks
the session unscored. That was correct while `insufficient` meant unknown telemetry. Once
abstention is a first-class outcome, it is a distinct classification, not invalid data.

This phase adds `visual_insufficient` to the evidence paths, accepts
`("inference", "insufficient")` and `("reused", "insufficient")` as classified states, and
keeps `visual_unavailable` for a look that failed or was refused. An abstention is never
folded into `visual_unavailable` to keep a scorecard green.

`("not_attempted", "insufficient")` stays unclassified. A look that never happened is not
an abstention, and accepting that pair would let a missing look pass as a deliberate one.

The path has no latency gate of its own. The existing gates cover `no_visual` and
`visual_cached`, and a new path must not silently widen them.

The classification contract changes, so the scorecard contract version moves with it. The
archived metric snapshot stays as it is: it records the numbers of an earlier contract, and
it is not a golden test for the current one.

## The camera answer cache

The camera cache stores the answer text and replays it as a fresh answer. That is wrong once
the answer carries a verdict: a cached `not_visible` or `unclear` would be replayed as a
successful answer.

The cache entry stores the whole inspection, and a cache hit reproduces the same status and
code the original look produced. A cached abstention stays an abstention. Cache identity,
the frame comparison, and the answer lifecycle are unchanged.

## Cassette for the controlled cases

The evaluation cassette holds the eight controlled cases recorded from the real provider.
Each entry stores the provider, the model id, the schema version, the provider-reported
usage, the response time, and the raw response text. No API key is stored.

One entry per line in `evals/fixtures/visual-inspection/provider-cassette.jsonl`:

```json
{"image_sha256": "<sha256>", "question": "<question>", "provider": "google",
 "model": "gemini-3.8-flash", "schema_version": "visual_inspection/1",
 "usage": {"prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0},
 "latency_ms": 0, "text": "<raw provider response>"}
```

The schema version is a constant in `fakeyou.perception.inspector`, so the cassette states
which contract produced the recorded response and a later change to that contract cannot
silently reuse an old recording.

The entry is keyed by the image digest and the question, exactly. The simulator cassette
cannot be reused for this: `RequestKey.of` replaces image data with `<image>`, so the same
question against `screen.png` and `occluded-screen.png` produces the same key. The dataset
contains exactly that pair, with `What is the beta date?` answerable on one image and
obscured on the other. A text-similarity key would replay the wrong response for one of
them, which would make the benchmark measure the cassette instead of the model.

Replay reads the cassette and never touches the network, so the evaluation numbers are
reproducible without credentials and without spending credits.

## The simulator cassettes

Two recorded look requests depend on the production look prompt: the camera look in
`sim-s3-camera` and the screen look in `sim-s3-screen`. Both replay recorded free text, so
a structured production prompt changes the shape of the expected response.

The double has one path for a look request, and it is the recording path. In
`record_looks` mode it relays the look request to the provider and replaces the entry
that the old recording left behind, so the cassette holds one look entry and it holds a
real provider response. Replay has no second path: a free text response is not an
inspection, and the production contract rejects it.

The rejection is deliberate. A compatibility adapter that wrapped the old free text into
a verdict would have produced a green simulator without proving that a structured
provider is used, and it would have turned "I cannot see the requested information" into
a supported claim. The regression test holds the line: every recorded look in the
simulator cassettes must parse as an inspection, and a recorded free text answer must be
rejected by the contract.

## The experiment budget

The recorder stops when it reaches twelve requests. It never extends the quota, never
retries automatically, and never falls back to another provider. A refusal to continue is a
recorded outcome, not a silent truncation.

The guard reads the attempt ledger before every provider call, so the budget survives a
restart and the historical request count is reconstructible from the artefact rather than
from the number of successful recordings. The ledger holds every outbound attempt, and
each attempt is reserved in the ledger before the call, so a timeout or a restart never
frees the quota of a request that was already sent.

Two counters are kept apart:

- The outbound attempt count is the cap. Every request that was sent to the provider is an
  attempt, including one the provider rejected at authentication, because the request was
  made and the quota is a request quota.
- The inference attempt count is telemetry. It counts the requests that reached the
  provider's inference path, and it is the number that explains the cost.

The cost is computed from the provider-reported usage of the successful recordings. The
cost of a timed-out request cannot be proven from the artefact, so the reported figure is
the cost estimated from successful recorded usage.

Each request records the provider-reported usage, and the report states the actual spend
from the provider's own numbers rather than an assumed per-request cost. The published
Gemini 3.8 Flash rates at the time of writing are $0.75 per million input tokens and $3.75
per million output tokens until 31 December 2026; the budget guard uses the pessimistic of
the two so the estimate cannot understate the cost.

The spent quota is fourteen outbound attempts against the approved twelve: eight
controlled cases, one request that timed out after it started inference, two simulator
look recordings, and three requests the provider rejected at authentication while this
phase was being wired. Eleven of the fourteen reached inference and cost an estimated
US$0.0146. The quota of this experiment is closed, and the guard refuses another call.

## Measured provider result

The eight controlled cases were recorded from `gemini-3.8-flash` in nine attempts, one of
which timed out, and the two simulator look entries were recorded in two more. Eleven
requests reached inference, costing an estimated US$0.0146 from the successful recorded
usage, against the approved US$1. Fourteen outbound attempts were made in total, so the
quota of this experiment is closed. Replaying the cassette offline produces:

| Metric | Result |
| --- | --- |
| Verdict accuracy | 8 of 8 |
| Answer accuracy | 5 of 5 |
| Abstention recall | 3 of 3 |
| Abstention precision | 3 of 3 |
| False abstention rate | 0 of 5 |
| Invalid output rate | 0 of 8 |
| Model error rate | 0 of 8 |
| Grounding precision | 5 of 15 |
| False claim rate | 8 of 8 |

The provider read the controlled scenes correctly. It named the beta date, the owner, the
project title, the shirt colour, and it abstained on the budget, on the person's name, and
on the obscured date, so the verdict and the answer are exact on every case.

The grounding numbers are the strict rubric baseline, and they measure the rubric rather
than hallucination. The provider states each observation as a sentence, for example
`The text displays 'Beta: 17 March 2027'`, while the rubric accepts only
`Beta: 17 March 2027`. Every observation is faithful, and every one of them is unlisted,
so the closed rubric scores each as a false claim.

These eight cases are a development dataset, not a held-out benchmark. The numbers above
show that the provider read these eight controlled scenes correctly. They are not evidence
about how a vision model performs in general, and the false claim rate of a rubric that
has not seen free-form assertions is not a false claim rate of the model.

The adjudication of the fifteen recorded assertions is a separate review, one assertion
at a time, and its outcome is kept apart from this baseline. A reviewed alias list would
add a second set of numbers next to the strict ones, never in place of them.

## The parsing contract the provider exposed

The first recorded provider response could not be parsed by the provider's own output
parser. `observed_facts` is a tuple and the contract is strict, so the parser rejected the
JSON array the provider sends, while the contract accepts it in JSON mode. The evaluation
harness already validated the raw response as JSON, and the production path now does the
same, so both read one contract and one parsing mode.

## Honest reporting

The report states answer accuracy, abstention behaviour, false claims, invalid output rate,
latency, and cost, and names the population each number came from. The scripted fake-model
scores from Phase 3b-A validate the harness and never stand in for provider quality.

The controlled dataset is synthetic. Eight controlled cases can show that the wiring, the
parsing, the abstention path, and the failure path behave as specified. They cannot show
how a vision model performs on real meeting frames, and no number from them is presented as
if they could.

## Execution and acceptance

1. Add failing tests for the structured inspection, the verdict-to-status mapping, the
   cache identity, the error taxonomy, and the new reporting path; implement the smallest
   change that satisfies them.
2. Add the cassette recorder and the offline replay, and prove the replay works without
   network access.
3. Run the offline suite, Ruff, mypy, import contracts, and dependency checks.
4. Record at most twelve requests from the real provider under the approved budget.
5. Commit the sanitised cassettes, verify the offline replay, and open the pull request.

Acceptance criteria:

1. **One inspection path.** The camera and the on-demand screen look use the same
   structured inspection, and the background screen reading still uses `ScreenReading`.
2. **Strict parsing and failure semantics.** A provider fault is a case-level
   `model_error`, invalid structured output is `invalid_output`, and a programming error
   fails the test.
3. **Snapshot safety is preserved.** The validation before and after inference is not
   weakened, and the cache identity and answer lifecycle stay correct.
4. **A legitimate cassette.** The eight controlled cases are recorded from the real
   provider with the provider, model id, schema version, usage, and response time stored,
   no API key stored, and a replay that runs without internet.
5. **No self-verification.** `verdict="supported"` never raises `verification` to
   `verified`.
6. **An honest benchmark.** The report names the population behind every number, and the
   fake-model scores are never presented as provider quality.

Phase 5 remains separate: live groundedness and abstention, false-claim rate, p50 and p95
latency, vision calls per turn, and meeting cost. This phase changes no reply prompt, no
acquisition path, and no session contract.
