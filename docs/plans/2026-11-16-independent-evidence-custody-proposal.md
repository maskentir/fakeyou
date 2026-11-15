# Independent evidence custody proposal

Status: **EVIDENCE CUSTODY PENDING. PROPOSAL ONLY.** No deployment, bucket,
credential, retention policy or upload is created by this document. Existing
custody and writer decisions remain in force. PublicationContext OFF; Phase 5B
NO-GO; V6 NO-GO; zero new provider requests/spending; no production activation.

## Observed starting point

| Item | Observed evidence |
| --- | --- |
| Local archive | `.runtime/writer-spike/writer-spike-raw-eba185d7.tar.xz` |
| Size | 3,088,788 bytes |
| SHA-256, re-read on 2026-11-16 | `974bc6012f2a5c28ea2d9b264c3821948167b747ed05c15d18603f918faa4513` |
| Existing custody decision | `2026-11-15-architecture-decision-closure.md`: RustFS destination, private auditor access, at least 12 months retention; custody still pending |
| Tested implementation | `rustfs/rustfs:1.0.0-alpha.81@sha256:4ee605dfe7c6548d1fa1856357e8a1eccd929e3176acf933fafeba3ce09f9` |
| Capability finding | Governance and Compliance retained versions deleted by ordinary delete; upload-time retention ignored; **P0** on the tested image |
| Bypass versus cleanup | Explicit bypass-header probe in step 12; cleanup in step 15 used ordinary delete without bypass |
| Independence | Existing instance and archive share the author's host/disk. Existing report establishes neither independent custody nor auditor reachability |

The hash was read locally; no storage API was called for this proposal. The
archive has not been uploaded. This document does not re-run retention tests
or assert that the existing service/bucket state was re-observed today.

## Options for authorization

| Option | Proposed placement | Tradeoff and decision boundary |
| --- | --- | --- |
| A: independently operated RustFS | Separate host/storage failure domain, separate administrator and credentials, private auditor access | Preserves the existing RustFS direction. No version is asserted safe. A pinned candidate must first demonstrate enforced retention; the tested alpha.81 is ineligible on present evidence |
| B: different independently operated immutable object store | Separate storage and administrator, with verified retention and auditor access | Alternative if A cannot meet the controls. Changing the RustFS destination or introducing a cloud dependency/spend needs an explicit new decision; none is authorized here |
| C: retain local archive only | Current author machine | Current state while authorization is pending. It does not satisfy custody; a second folder, volume or bucket on that host is not an independent copy |

Recommended for review: A, conditional on successful independent retention and
access verification, with B available only through an explicit revised storage
decision. Do not deploy alpha.81 elsewhere and assume separation fixes its
retention behavior. Do not upload evidence first and test immutability later.

## Required custody boundary

The proposed destination must survive loss of the author's machine and local
Docker volume. Separate credentials alone are insufficient if the same host or
unrestricted administrator can delete both copies. Document the operator,
infrastructure owner, failure domain, backup/deletion authority and recovery
responsibility before calling the placement independent.

Proposed roles, with named people assigned only by an authorization decision:

| Role | Proposed access |
| --- | --- |
| Evidence uploader | Create evidence objects at the approved prefix; no delete, retention-shortening or governance-bypass grant |
| Custodian | Maintain storage/access and record custody events; no claim of immunity from infrastructure administrators |
| Auditor/reviewer | Private read of the pinned object version and manifest; no public/presigned link, shared drive or PR attachment |
| Retention administrator | Separate documented authority for approved policy; actions logged outside the uploader's control |

TLS, non-default credentials, encryption at rest, private access and key recovery
must be demonstrated for the actual destination. No implementation or current
product capability is assumed from an API's accepted configuration response.
Do not put credentials, raw PCM, recordings or the archive into GitHub.

## Proposed acceptance sequence after authorization

These steps are a future runbook, not operations performed in this session.

1. Approve operator, destination, retention mode and period, access list,
   infrastructure cost ceiling and independent failure domain. Preserve the
   minimum 12-month period in the current ruling unless explicitly superseded.
   Retention's irreversibility and infrastructure administrator powers must be
   acknowledged; provider/model-spend authorization is not implied.
2. Approve a separately bounded storage capability test using synthetic text
   only. Pin the server image/build and client; record all request parameters,
   version IDs, server times, return codes and subsequent read/list results.
   Failed deletions are verified by version-specific reads, not by exit code
   alone. A cleanup plan must not rely on breaking the retention being tested.
3. Demonstrate versioning and upload-time retention, not only metadata set by a
   later API call. Verify retained version IDs survive attempted deletion and
   remain readable through the approved retention window/test procedure.
4. Verify Governance behavior separately: ordinary delete is refused; an
   uploader cannot use bypass or shorten retention; any authorized bypass probe
   uses a separately disposable synthetic version and is recorded distinctly.
   A working bypass is not evidence of Compliance retention.
5. Verify Compliance behavior separately: ordinary and bypass-header deletion
   and retention shortening fail for the relevant roles while retention is
   active. Document infrastructure-level deletion/key-loss risk; do not infer
   legal compliance or absolute immutability from API controls.
6. Re-read versions, hashes and retention after a controlled restart; verify
   auditor access from the approved independent environment. Test denied public,
   wrong-principal and write/delete access without publishing temporary links.
   Record untested failure/recovery behavior explicitly.
7. Obtain a separate upload authorization for the exact archive digest, private
   prefix and object version. Verify the local hash again, upload without
   transforming the archive, then download that exact stored version through
   authorized auditor access and compare SHA-256 and size. ETag alone is not the
   content digest. Keep the local copy.
8. Record uploader/custodian identity, UTC timestamps, endpoint/deployment build,
   bucket/key, object version, retention mode/until, encryption/access evidence,
   local and downloaded hashes, size, and the independent review outcome. Store
   the non-secret manifest/reference in the repository; raw archive stays private.
9. Only after that review may the responsible human authorize changing
   `EVIDENCE CUSTODY PENDING`. An upload receipt by itself does not close custody.

## Proposed negative controls, not executed

| Control | Required result |
| --- | --- |
| Stop author machine / lose local Docker volume | Authorized auditor can still retrieve the exact remote version |
| Ordinary deletion of a retained version | Refused; version-specific read and hash unchanged |
| Uploader supplies bypass or attempts retention shortening | Refused by the approved role policy; original retention unchanged |
| Compliance deletion with explicit bypass header | Refused for the tested principals; version remains readable |
| Upload with retention headers | Effective retention confirmed on the exact uploaded version, not an empty result |
| New object version or delete marker | Original pinned version remains retained/readable; latest-key lookup cannot substitute evidence |
| Restart or credential rotation | Original versions and approved auditor read access remain verifiable |
| Public, anonymous or wrong-principal access | Denied; no public/presigned link introduced |
| Wrong version, size, checksum or altered archive | Verification fails; custody stays pending |
| Auditor has no reachable independent endpoint | Custody stays pending, even if local operations succeed |

## Evidence limits and decisions still required

The writer archive can support clip presence and observer delivery gaps. It
cannot reconstruct every write/resume instant; those were not persisted per
clip. Independent custody will not turn it into complete per-clip timing proof,
reverse the rejected writer decision, satisfy V6, or authorize another writer
experiment. The limitations in the closure remain attached to its manifest.

Required decisions: destination/operator and independence; approved build and
storage test scope; retention/access/key policy and cost ceiling; exact upload
authorization; independent verification and final custody acceptance. None is
inferred from approval to prepare this proposal. No deployment was provisioned,
no archive uploaded, and **EVIDENCE CUSTODY PENDING** remains unchanged.
