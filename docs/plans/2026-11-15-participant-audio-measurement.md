# Production readiness 3/5 — Consent-aware participant audio measurement

Base: `feat/evidence-capture-provenance` (2/5). Offline only. This PR adds a
contract and deterministic fake adapters. It adds no real participant observer.

## Why a contract first

`measure_publication` needs an independent observation that a participant heard a
sentence. Nothing in production can supply one. Before any adapter is built, the
rules for when such an observation may be used have to be fixed, so that an
adapter cannot turn a local write, an unconsented recording, or an unmapped clock
into a compliant verdict.

## Contract

`ParticipantAudioObserver` is the port an adapter implements: its observer ID, the
consent record, what it heard for a sentence binding, and the mapping from its
clock to the measurement clock.

`ParticipantConsent` records the observer, the state (`granted`, `denied`,
`unknown`, `revoked`), who attested it, the scope, and when it was granted and
revoked on the measurement clock.

`observe_participant` turns an observer into a usable observation only when every
condition holds. Each failure has its own explicit gap:

| Gap | Condition |
| --- | --- |
| `participant_observer_unavailable` | no observer |
| `participant_observer_mismatch` | consent or report belongs to another observer |
| `participant_consent_unknown` | state unknown, or no attesting source |
| `participant_consent_denied` | consent denied |
| `participant_consent_revoked` | revoked, or revoked at or before the sentence ended |
| `participant_consent_scope_mismatch` | consent given for a different purpose |
| `participant_consent_not_in_force` | no grant time, or granted after the sentence started |
| `participant_sentence_not_reported` | the observer reported nothing for this sentence |
| `publication_identity_unverified` | the sentence was matched by an unverified method |
| `participant_clock_unmapped` | the observer clock has no mapping to the measurement clock |

Without standing consent the observer is never asked what it heard. Verified
identity methods are `watermark` and `fingerprint`; a transcript or timing match
is not accepted. The observer clock uses the same `RemoteClock` mapping as 2/5;
mapping uncertainty above 50 ms stays `participant_clock_unmeasurable`.

`measure_participant_publication` composes this with the existing measurement and
with the playback receipt from 1/5:

- The local write flag is read from the receipt of the same publication ID:
  written audio is true, a settled receipt without audio is false, anything else
  is unknown. A receipt for another publication says nothing.
- A gap never produces a compliant verdict. The result always carries
  `participant_gap` and `participant_observer_id`.
- A gate veto with a valid consented observation stays `observed_noncompliant`.
- A gate veto with a no-write receipt and no report stays `prevented_*`.
- A no-write receipt contradicted by a participant report stays a conflict.

## What exists and what does not

Exists: the port, the consent rules, the composition, and
`ScriptedParticipantObserver`, a deterministic fake in `tests/`.

Does not exist: any production implementation of `ParticipantAudioObserver`, any
consent capture flow, any watermark or fingerprint matcher, and any measured
remote clock exchange with a participant device. In production every measurement
therefore reports `participant_observer_unavailable`. The fake's verdicts are
controls for the contract, not measurements of a meeting.

## Deterministic proof

`tests/test_participant_audio.py` (58 tests): the compliant control, every consent
gap against allowed and vetoed decisions, no read without standing consent,
observer and identity mismatches, unverified identity methods, unmapped, stale,
and uncertain clocks, veto with observation, veto with a no-write receipt, the
receipt-to-local-write mapping, and the conflict case.
