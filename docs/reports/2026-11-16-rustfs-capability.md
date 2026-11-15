# RustFS capability report — private evidence storage

Status: **CAPABILITY TEST ONLY. NOT APPROVAL FOR EVIDENCE CUSTODY. `fakeyou-evidence` NOT CREATED.**

Ruling of 2026-11-15: the existing RustFS instance is authorized for isolated
capability testing only. The final evidence bucket `fakeyou-evidence` must not
be created until an independent deployment, security controls, retention and
auditor access are verified. This report records what the local instance can
and cannot do.

Every storage call used `--endpoint-url http://127.0.0.1:9000` explicitly, with
`AWS_EC2_METADATA_DISABLED=true` and no AWS profile, so no request could reach
AWS. Credentials were read from `.env` into the environment and never printed.
No real evidence archive was uploaded; every object written was a synthetic
one-line text probe.

## Environment and image identity

| Item | Value |
| --- | --- |
| Service | `rustfs` in `compose.yaml` |
| Image | `rustfs/rustfs:1.0.0-alpha.81@sha256:4ee605dfe7c6548d1fa1856357e8a1eccd929e3176acf933fafeba3ce09f9` |
| Container | `fakeyou-rustfs-1` |
| Data | Docker volume `fakeyou_rustfs_data`, mounted at `/data` |
| Ports | `127.0.0.1:9000` and `127.0.0.1:9001`, loopback only |
| Server before the test | stopped |
| Server after the test | stopped, restored |
| Existing bucket | `fakeyou`, 1,000 objects and 1,000 versions, untouched before, during and after |
| Test bucket | `fakeyou-evidence-capability-test-20261115`, created and deleted during the test |

The test bucket was empty when created and held only the probes below. The
existing `fakeyou` bucket was read for counts before and after and never
written.

## Operations performed

| Step | Operation | Result |
| --- | --- | --- |
| 1 | `list-buckets` | `fakeyou` only |
| 2 | `head-bucket` on the test bucket | 404, did not exist |
| 3 | `create-bucket --object-lock-enabled-for-bucket` | created |
| 4 | `get-bucket-versioning` | `Enabled` |
| 5 | `get-object-lock-configuration` | `ObjectLockEnabled: Enabled` |
| 6 | Two `put-object` calls on one key | two distinct version IDs, `IsLatest` correct on the second |
| 7 | `get-object` for each version and for the latest | each returned its own content |
| 8 | `put-object` with `--object-lock-mode` at upload | retention was **not** applied; `get-object-retention` returned an empty object |
| 9 | `put-object-retention` with `Mode=GOVERNANCE` | accepted; `get-object-retention` returned the mode and the date |
| 10 | `delete-object --version-id` on the Governance-retained version | **succeeded**; the version disappeared from `list-object-versions` |
| 11 | `put-object-retention` with `Mode=COMPLIANCE`, then `delete-object --version-id` | **succeeded**; `get-object` afterwards returned `NoSuchVersion` |
| 12 | `delete-object --bypass-governance-retention` | succeeded, as an ordinary delete |
| 13 | Controlled restart of the container | health 200 after restart; versioning, Object Lock configuration, versions and retention metadata all still present |
| 14 | Re-read both versions after the restart | each returned its own content |
| 15 | `delete-object` for every remaining version, then `delete-bucket` | all removed, bucket deleted; `fakeyou` untouched |

## Verdicts

| Question | Verdict | Basis |
| --- | --- | --- |
| Versioning | **established** | Two versions of one key with distinct IDs; the older version readable by ID and not latest; both readable after a restart |
| Object Lock can be enabled at bucket creation | **established** | The flag is accepted and reported as enabled |
| Retention metadata can be written and read | **established** | Set through `put-object-retention`, read back with mode and date, still present after a restart |
| Retention applied at upload time | **refuted** | `--object-lock-mode` and `--object-lock-retain-until-date` on `put-object` are accepted with exit 0 and then ignored; the object has no retention |
| Governance retention prevents deletion | **refuted** | A version with Governance retention until a future date was deleted successfully and vanished from the version list |
| Compliance retention prevents deletion | **refuted** | A version with Compliance retention until a future date was deleted; reading that version afterwards returned `NoSuchVersion` |
| Bypass header has a distinct meaning here | **refuted** | It behaves like an ordinary delete, and an ordinary delete bypasses retention too |
| Retention survives a restart | **established for the metadata only** | Mode and date are still readable after the container restart. Whether they would still block a delete after the restart is untested, and untestable while retention blocks nothing |
| Objects and versions survive a restart | **established** | All probe objects and both versions were present and readable after the restart |
| Data is durable beyond the host | **unproven** | The volume is on the same disk as the local archive it is meant to safeguard. No second copy exists |
| Reachability by an auditor | **refuted** | Ports are bound to loopback; no RustFS appears in the deployment manifests |
| Encryption at rest, or TLS to the endpoint | **unproven** | The endpoint used is plain HTTP; nothing in the deployment configuration or the tested surface establishes encryption at rest |

## Cleanup status

| Item | State |
| --- | --- |
| Retained objects left behind | None that could not be removed. Retained versions were removed by ordinary delete, which is itself the finding above |
| Retention bypassed to clean up | No. Every removal used the ordinary `delete-object` call; no header or administrative override was used |
| Test bucket | Deleted; `list-buckets` shows `fakeyou` only |
| Existing `fakeyou` bucket | Untouched: 1,000 objects and 1,000 versions before and after |
| Docker volumes | Not deleted, not altered |
| Service state | Stopped, as before the test |
| Deployment manifests | Not changed |
| Ports | Not exposed beyond loopback |

## Operational and security limitations

1. **Retention is not enforced.** This is the decisive finding. Object Lock is
   configured and reported, but a retained version can be deleted with the
   ordinary API. An evidence store that cannot refuse a deletion cannot hold
   anything immutable, whatever its metadata says.
2. **The instance is local.** Loopback only, on the author's machine, sharing a
   disk with the copy it is meant to protect. It offers one copy, not two.
3. **The image is an alpha.** `1.0.0-alpha.81`. Behaviour may change between
   versions, and the retention gap may be a defect that is fixed or a design
   that is not.
4. **No independent deployment exists.** Nothing in the deployment manifests
   runs RustFS. An auditor has no reachable endpoint.
5. **Credentials are the deployment default.** `compose.yaml` falls back to
   `rustfsadmin` when `.env` does not set them. This instance runs with
   non-default values, but the default path remains in the manifest.
6. **No TLS.** The endpoint used is HTTP on loopback. For an auditor-facing
   deployment this would need revisiting.

## Consequence for evidence custody

`fakeyou-evidence` was **not** created. Two findings block it:

- the implementation does not enforce Object Lock retention, so the immutability
  the audit trail depends on cannot be claimed, whatever the metadata shows;
- there is no deployment an auditor can reach, and no second copy of the
  archive.

The archive stays where it is, `.runtime/writer-spike/writer-spike-raw-eba185d7.tar.xz`,
3,088,788 bytes, SHA-256 `974bc6012f2a5c28ea2d9b264c3821948167b747ed05c15d18603f918faa4513`,
verified read-only on 2026-11-16. Custody remains pending.

What would unblock it: a RustFS deployment an auditor can reach, with retention
verified to refuse a deletion on that deployment, versioning confirmed, Object
Lock and its retention fixed at bucket creation, credentials not default,
encryption at rest, and a defined access list. That is a new deployment and a
new authorization, not a configuration of this one.
