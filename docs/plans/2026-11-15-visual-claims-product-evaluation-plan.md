# Proposed product evaluation: visual claim proposal and liveness

Status: **PLANNING ONLY — NO PROMPT/SCHEMA/RUNTIME CHANGE OR PROVIDER EXPERIMENT**.

This workstream is separate from the NC-76/I7 amendment, page-epoch fix and
physical-output readiness. Current authorization remains deterministic local
facilities with zero new provider requests and US$0. No meeting, participant,
new remote model/TTS experiment, dataset collection, activation or merge is
performed by this plan.

## Repository-grounded gap

The agent already has structured output: `conversation/agent.py` uses
`ProviderStrategy(SpeechPlan)`. `conversation/reply_planner.py` defines
`SpeechPlan.claims` using `session.speech.ClaimHeader` plus `say`; `SpeechPlan.text`
joins the `say` strings. `SpokenChunk` carries text and language.

That structure is not the visual claim schema expected by
`foundation/claims.py::parse_claims`, which searches text for `CLAIMS:` and parses
`kind`, `shape`, field values, polarity and dependence. `VisualTurn.speak` uses
that parser. The tested `tests/test_publication_activation.py::with_claims`
helper appends a labelled marker by hand. Neither the agent/persona guidance nor
that helper proves that a real model emits and preserves the visual proposal
format through the actual adapters.

The EN result 7/19 (36.84%) measures labelled proposal support/renderability under
answer-only evidence. It does not measure proposal emission probability or the
complete question → model schema → adapter → visual matcher → renderer path.
False support 0/22 is limited to that labelled set. ID cassette look coverage is
unmeasured; synthetic ID controls are a separate evidence class.

## Separate measurements and denominators

| Stage | Denominator / measurement | Failure accounting |
| --- | --- | --- |
| Eligible question | Recorded visual questions with an identified look and response | Missing/insufficient/unavailable look, source and language labelled separately |
| Proposal availability | Eligible turns that actually preserve a parseable visual proposal through the real adapter | No marker, wrong schema, lost fields, malformed/truncated JSON, unsupported shape |
| Support | Proposed claims with enough original look evidence to check L1–L11 | Missing relation/value/entity, polarity, ambiguity, wrong evidence kind/subject |
| Rendering | Supported proposals that pass G1–G8 and have a reviewed EN/ID pattern | Field grammar/length, unsupported language/shape, no current camera pattern |
| Authorized availability | Renderable statements with an authorized local/pre-existing audio path | No synthesis grant/path; provider mode remains denied |
| Publication | Candidates evaluated at final commit and receipted by the writer | Freshness/continuity/identity/time failures and zero-byte/cancelled outcomes |
| Product answer coverage | Eligible correct-answer turns that reach an appropriate supported runtime statement | Component abstention does not count as answering the original question |

Report false support, false rejection, supported/renderable coverage, product
answer coverage, abstention and latency by EN/ID, evidence source and failure
stage. A denominator with no samples is unmeasured, never zero failure. Do not
combine synthetic protocol cases with recorded model behavior or report a local
receipt as participant audibility.

## Next offline evaluation scope

1. Inventory existing cassettes read-only with path, row identity and SHA-256.
   Identify which rows contain actual structured agent responses, which contain
   look answers, and whether they form linked visual turns. Record missing links;
   do not reconstruct a supposedly observed model reply from labels.
2. Replay the actual preserved agent response through the actual schema and
   adapter path into `parse_claims`. Do not append `CLAIMS:` in the measured
   model-behavior lane. Report its availability separately from matcher coverage.
3. Keep an explicitly synthetic protocol lane for EN/ID valid and malformed
   `CLAIMS:` blocks. Exercise stream chunk boundaries, quoted JSON, wrong kinds,
   undeclared claims, and discarded model wording with local fake synthesis.
   Those controls prove plumbing only, not model compliance.
4. Compare two **design candidates**, without deploying either:
   - Preserve typed visual claim metadata in the structured response and through
     `SpokenChunk`/adapters, avoiding serialization into text intended for speech.
   - Use a precisely specified `CLAIMS:` JSON protocol in the response/prompt,
     with runtime rejection of missing/malformed proposals and complete exclusion
     of metadata/model sentences from visual synthesis.
   Both need separately reviewed schema/prompt/adapter changes and negative
   controls before implementation. Existing session claim headers are not
   automatically equivalent to visual evidence claims.
5. Keep support checks unchanged. A missing entity/attribute relation in a short
   look answer cannot be supplied from the question or inferred semantically to
   make coverage rise. Diagnostic `answer_and_observed_facts` results cannot be
   silently promoted into the runtime evidence contract.
6. Publish new versioned reports and manifests. Preserve all prior labels,
   cassettes, fixtures, scorecards, ledgers and SLOs. Record component and
   end-to-end denominators, not only the most favorable lane.

No new model output can be collected under this authorization. If existing
cassettes cannot establish real proposal emission, that result remains
unmeasured. Future empirical model/prompt evaluation needs explicit provider,
request-count and budget authorization; synthetic mocks cannot fill that gap.
Likewise, creating adequate new EN/ID evidence or labels from new private
meeting data requires its own authorization and privacy review.

## Product decision gates retained

- Product liveness remains NO-GO at the current 36.84% labelled EN coverage and
  absent ID cassette evidence. A contract amendment alone does not close it.
- A minimum useful coverage/latency threshold needs product review before a
  future comparison is called successful; this document creates no SLO change.
- No raw visual model text reaches synthesis, regardless of the chosen proposal
  protocol. Support, source/subject identity, grammar and final output gates hold.
- V6 physical writer bounds, Y2 physical provider accounting and participant
  consent/clock/audibility gates remain independent and unresolved.
- No provider experiment, prompt/schema implementation, new data acquisition or
  activation is included in approval to publish this planning document.
