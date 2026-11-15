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
| `participant_consent_not_in_force` | no grant time, or granted after the earliest possible audio start |
| `participant_consent_reference_missing` | no trusted gate time or query time to prove the grant against |
| `participant_consent_clock_incomparable` | consent, reference, or observer clock are in different or unknown domains |
| `participant_sentence_not_reported` | the observer reported nothing for this sentence |
| `publication_identity_unverified` | the sentence was matched by an unverified method |
| `participant_clock_unmapped` | the observer clock has no mapping to the measurement clock |

The observer is asked what it heard only after two checks pass, both without
touching its data.

The consent record must be complete: state granted, an attesting source, the
measurement scope, a finite grant time, and no revocation at or before the grant.

The grant must then be proven against a trusted `ConsentReference` in the same
clock domain as the consent record:

- `audio_not_before` is the earliest moment the sentence's audio can have
  existed. It is the gate decision time minus its uncertainty, because the gate
  runs before the first PCM write. The grant must be at or before it.
- `queried_at` is the trusted time of the read. A grant in the future relative to
  it is not in force, and consent revoked at or before it permits no new read.

A missing or non-finite reference time, or a query time before the audio bound,
is `participant_consent_reference_missing`. A consent record without a clock
domain, or one in a different domain than the reference or the mapped observer
clock, is `participant_consent_clock_incomparable`. In each case `heard()` is
not called.

Revocation has two separate rules. A report read while consent was in force
stays usable when the revocation came after the sentence ended. A read attempted
after the revocation is never made, even for a sentence that ended before it. A
revocation at or before the sentence end still discards a report that was read
earlier. Verified
identity methods are `watermark` and `fingerprint`; a transcript or timing match
is not accepted. The observer clock uses the same `RemoteClock` mapping as 2/5;
mapping uncertainty above 50 ms stays `participant_clock_unmeasurable`.

`measure_participant_publication` composes this with the existing measurement and
with the playback receipt from 1/5:

- The local write flag is read from the receipt of the same publication ID:
  written audio is true, a settled receipt without audio is false, anything else
  is unknown. A receipt for another publication says nothing.
- A settled matching receipt is authoritative. A caller-supplied
  `local_write_observed` is used only when no matching receipt has settled, and
  can neither assert a write the receipt denies nor hide one it reports.
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

`tests/test_participant_audio.py` (93 tests): the compliant control, every consent
gap against allowed and vetoed decisions, no read without a grant proven against the trusted reference, no new read
after revocation,
no caller override of the receipt,
observer and identity mismatches, unverified identity methods, unmapped, stale,
and uncertain clocks, veto with observation, veto with a no-write receipt, the
receipt-to-local-write mapping, and the conflict case.
