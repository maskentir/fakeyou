# Production readiness 2/5 — Evidence capture and provider provenance

Base: `feat/playback-outcome-accounting` (1/5). Offline only. No publication
context is activated, so this changes what evidence records, not what is spoken.

## Gap closed

After #198 production evidence supplied neither a measured capture clock mapping
nor a provider attempt identity, and it reported the capture time as the
observation time. With those fields absent the gate could only answer
`capture_time_unmapped`, and measurement could only answer
`provider_identity_unmeasurable`.

## Capture clock mapping

A frame's capture timestamp is taken in the browser page clock. The gate compares
ages on the local monotonic clock. The mapping between the two is now measured,
not assumed:

- The perception drain already makes one page round trip per poll. The page
  script returns its own clock reading with each batch, and the drain records the
  local monotonic time just before and just after the call.
- `RemoteClock` keeps the recent exchanges. A page instant maps to the exchange
  midpoint shifted by the page-clock distance. The uncertainty is half the round
  trip plus a drift allowance of 200 ppm of that distance.
- The tightest usable exchange wins. An exchange older than 30 s, a page instant
  more than 120 s from every exchange, an invalid exchange, or a page script that
  does not report its clock all yield no mapping.

The evidence binding carries the mapped monotonic capture time, the uncertainty,
the mapping clock domain `local_monotonic`, and the origin `page_clock_exchange`.
When there is no mapping every one of those fields stays absent and the origin
stays `snapshot_ref_reported`. Nothing substitutes the observation time.

The existing gate limits are unchanged: uncertainty above 50 ms is
`capture_time_invalid`, and a capture older than 5 s is `capture_too_old`.

## Observation time

The observation time of an inference is now the local monotonic time at which the
provider call returned, in the `local_monotonic` domain. A result that never
reached the provider keeps its wall-clock timestamp and the `utc` domain label, so
the two cases stay distinguishable.

## Provider attempt identity

Each provider invocation for a visual inspection gets a locally generated attempt
ID, the configured `provider/model` label, and the provider's response ID when the
response carries one. A reused camera answer keeps the attempt that produced it.
A look that never reached the provider has no attempt. An interpreter built
without a provider label reports no provider, and measurement stays
`provider_identity_unmeasurable`.

`perception_evidence_resolved` journal rows gain `provider_attempt_id`,
`provider`, `provider_response_id`, `capture_mapped`, and
`capture_mapping_uncertainty_ms`. The model-facing evidence payload is unchanged.

## What this does not establish

- The page script change is a one-line addition that has not run against a real
  Meet page in this sprint. Until it does, live mapping quality is unmeasured.
- Drain round trips that carry large frame batches can exceed 100 ms. Those
  exchanges produce more than 50 ms of uncertainty and the gate will answer
  unknown. Whether live round trips are tight enough is a live measurement.
- A screen keyframe is captured when the screen changes. A static slide that has
  been up for more than 5 s is `capture_too_old` under the current limit even with
  a perfect mapping. This is recorded as an activation finding in 4/5, not changed
  here.
- The provider attempt ID is our request identity. It is not a provider-side
  audit record.

## Deterministic proof

`tests/test_evidence_provenance.py`: mapping arithmetic, drift, tightest exchange,
invalid and stale exchanges, the drain measuring the mapping, a page without the
clock field, production evidence carrying mapping and provider attempt, distinct
attempts, reused attempts, and the gate answering valid, `capture_too_old`,
`capture_time_invalid`, and `capture_time_unmapped` from real provenance. The
negative control with capture at 90 s, observation at 99 s, and write at 100 s is
invalid.
