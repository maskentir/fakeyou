# Publication decision and evidence archive

This is the canonical reading index as of 2026-11-16. It archives selected
historical records without importing their implementation ancestry. The
Markdown records and selected compact JSON are byte-identical to frozen source
[`d37b89f40155d6130ebda72de2d1ce12eeaf06b4`](https://github.com/maskentir/fakeyou/commit/d37b89f40155d6130ebda72de2d1ce12eeaf06b4).
The Markdown records, writer result JSON and #220 review JSON are byte-identical.
The two `-summary.json` files are new canonical selections derived from the
immutable source JSON; they omit raw traces, runtime snapshots and SDK invocation
logs while preserving source blob/hash references and raw artifact hashes. The
complete historical JSON remains available at those immutable refs.
Their dated status lines describe the time of execution; the current rulings
below govern where a later decision supersedes them.

PublicationContext **OFF**; Phase 5B **NO-GO**; V6 **NO-GO**. Contract
[#205](https://github.com/maskentir/fakeyou/pull/205) is unchanged. This extraction
makes zero new runtime provider requests and incurs zero provider spend. It
authorizes no activation, deployment, evidence upload or final custody bucket.

## Current decisions and supersession

- **ADR-2 decision closed:** both tested W-A2 placements are rejected for V6;
  full S1 trials are cancelled and protocol revision 4 is superseded. The
  [closure](../plans/2026-11-15-architecture-decision-closure.md) takes precedence
  over the earlier design/protocol. Confirmed daemon-stall violations support
  rejection; observations and unproven PF2/PF3/PF4 arguments remain separate.
- **Independent per-clip writer lateness replay is unavailable.** The raw
  recordings can reproduce clip presence and delivery gaps, but write/resume
  instants were held in memory and only aggregates were recorded. Acceptance of
  that limit supports rejection of these designs, never approval of another.
- **Evidence custody pending:** the archive remains local; no independently
  reachable, separate failure-domain store or final `fakeyou-evidence` bucket is
  established. Alpha.81 fails ordinary-delete retention enforcement. The
  [1.0.5 observation](2026-11-16-rustfs-1.0.5-local-capability.md) establishes
  narrow local API controls only. Its failed setups and original restart-port
  orchestration failure remain evidence; follow-up success does not erase them.
  Independent custody is tracked in [#227](https://github.com/maskentir/fakeyou/issues/227).
- **#220 residual risk accepted:** the latest co-founder **PR BACKLOG RESOLUTION
  & FOUNDATION DELIVERY** ruling, recorded in the merged
  [#220 disposition](https://github.com/maskentir/fakeyou/pull/220), supersedes
  the historical **MERGE HOLD** recommendations in the exact-head and rNext
  reports. #220 merged as
  [`70986cefccf62f0e6c5606bd37c0246df4c75e5c`](https://github.com/maskentir/fakeyou/commit/70986cefccf62f0e6c5606bd37c0246df4c75e5c).
  Acceptance is limited to the non-activated 14-file perception foundation.
  The intermittent early-commit failure might recur; cause, harmlessness and
  transitive influence remain unproven. Failed executions remain failed, and
  attribution remains **INCONCLUSIVE**. Deterministic early-commit testing is
  an open obligation in [#223](https://github.com/maskentir/fakeyou/issues/223),
  no longer an automatic merge dependency. This accepts no publication safety
  claim and waives no #205 requirement.
- **ADR-1, ADR-3 and NC-76/I7 remain proposals:** direction acceptance is not
  an approved contract amendment or activation permission. The preparation
  amendment remains explicitly draft, unapproved and unimplemented. Subsequent
  scoped work is tracked separately in
  [A #224](https://github.com/maskentir/fakeyou/issues/224),
  [B #225](https://github.com/maskentir/fakeyou/issues/225) and
  [C #226](https://github.com/maskentir/fakeyou/issues/226).

## Selected records

| Record | Role and reading limit |
| --- | --- |
| [ADR-2 closure](../plans/2026-11-15-architecture-decision-closure.md) | Final rejection basis and pending custody; governs the earlier writer proposal |
| [Architecture review](../plans/2026-11-15-architecture-decision-review.md) | ADR-1/ADR-3 and proposed invariants; earlier ADR-2 design is historical |
| [Writer protocol](../plans/2026-11-15-writer-spike-protocol.md) | Superseded revision 4, retained to reproduce the original experiment's intent |
| [Preparation amendment](../plans/2026-11-15-local-preparation-contract-amendment-proposal.md) | Proposed NC-76/I7 wording and controls; no approved contract change |
| [Writer preflight](2026-11-15-writer-spike-preflight.md), [JSON](2026-11-15-writer-spike-preflight.json) | Environment, harness/code hashes, results and raw-file manifest/checksums; no raw recordings included |
| [Alpha.81 capability](2026-11-16-rustfs-capability.md) | Retention failure and corrected distinction between bypass probe and ordinary cleanup; image digest erratum below |
| [#220 exact-head review](2026-11-16-pr220-exact-head-review.md), [JSON](2026-11-16-pr220-exact-head-review.json) | Original failures, CI, commands and artifact hashes; historical merge recommendation superseded above |
| [rNext differential](2026-11-16-rnext-differential.md), [JSON](2026-11-16-rnext-differential-summary.json) | All four runs, selected causal timestamps, artifact hashes and attribution limits; historical merge recommendation superseded above |
| [RustFS 1.0.5](2026-11-16-rustfs-1.0.5-local-capability.md), [JSON](2026-11-16-rustfs-1.0.5-local-capability-summary.json) | All setup attempts, orchestration failure, follow-up, selected result metadata, artifact hashes and local-only limits |

These are decision and execution records, not duplicate superseded implementation
plans. Links or commands naming local temporary paths describe historical
execution; they do not claim an auditor can access those artifacts. No PCM,
archive, raw execution log or credential is committed here.

## Exact provenance and integrity

The [archive manifest](2026-11-16-publication-canonical-archive-manifest.json)
records source commit, Git blob, byte length and SHA-256 for each of the 11
unchanged historical records. JSON companions preserve experiment metadata,
raw-file checksums, commands, result counts and failure provenance. A manifest
is not independent custody and cannot recreate missing write/resume timestamps.

| Historical source | Exact reference |
| --- | --- |
| Extraction source / rNext record [#222](https://github.com/maskentir/fakeyou/pull/222) | `d37b89f40155d6130ebda72de2d1ce12eeaf06b4` |
| Frozen exact-head audit [#221](https://github.com/maskentir/fakeyou/pull/221) | `44a5739ea74ff2ebdd38ae54b768e36dc5fd593a` |
| Earlier closure/capability [#218](https://github.com/maskentir/fakeyou/pull/218) | `917c56dc546930841cb8b0ab39dc7ce4a4cd5716` |
| Preparation proposal [#217](https://github.com/maskentir/fakeyou/pull/217) | `4f5c22e2c9cd5bf3e9f294630851ba75c938e363` |
| Writer harness execution | [`eba185d7c4c6ac93a897d57e8eb192bceb31059f`](https://github.com/maskentir/fakeyou/tree/eba185d7c4c6ac93a897d57e8eb192bceb31059f/evals/writer_spike) |
| Reviewed #220 head | `77e51c797ddbc9df75a5eeeac73331d306cdd2e1` |
| Earlier equivalent #220 tree commit | `36da8ca296b59920e01b4189a647ec6376eff56b` |
| Pre-#220 baseline | `a79197b5cf20d4235861b46edeb20165f4112bed` |

Original protocol and harness are available at their immutable refs, including
[the superseded protocol at #218](https://github.com/maskentir/fakeyou/blob/917c56dc546930841cb8b0ab39dc7ce4a4cd5716/docs/plans/2026-11-15-writer-spike-protocol.md).
The original branches/PRs and refs are preserved; this archive does not merge
or close their historical ancestry.

**Attributed image-digest erratum:** the historical alpha.81 capability report
prints a truncated digest ending `...ce09f9`. Its bytes are deliberately
preserved. The actual Compose pin at both the frozen source and archive base is
`rustfs/rustfs:1.0.0-alpha.81@sha256:4ee605dfe7c6548d1fa1856357e8a1eccd929e3176acf933fafeba3ce09a69f9`.
See [Compose at the extraction source](https://github.com/maskentir/fakeyou/blob/d37b89f40155d6130ebda72de2d1ce12eeaf06b4/compose.yaml).
This corrects identification only; no storage operation was rerun and no
historical result is rewritten.

The raw writer archive is identified, not included:
`writer-spike-raw-eba185d7.tar.xz`, 3,088,788 bytes, SHA-256
`974bc6012f2a5c28ea2d9b264c3821948167b747ed05c15d18603f918faa4513`.
Reproduction requires the historical harness and the local raw artifacts;
independent custody and per-clip lateness reconstruction remain unavailable.
