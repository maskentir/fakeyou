# Phase 5A Hardening — final offline audit

Contract: merged PR #205, `docs/plans/2026-11-15-publication-safety-liveness-contract.md`.
Base: `a79197b5cf20d4235861b46edeb20165f4112bed`. Audit parent: PR #215,
`dfb775b7f48b1e18fdb0337b21a87dc020ad37f5`. The audit head is recorded on its draft PR.

**Offline acceptance: NOT COMPLETE. Activation: NO-GO.** All 119 negative controls
are accounted for: 94 covered by deterministic offline assertions, 24 partial,
and one confirmed contract mismatch (NC-76). A passing test suite does not turn
partial controls into passing acceptance criteria. Production readiness and
compliance are not claimed. No PR was marked ready or merged.

PublicationContext remains OFF. Phase 5B remains unauthorized. No real meeting,
live Meet page, participant observer, external inference/TTS request, or paid
experiment was performed. New provider requests: 0. New provider spend: US$0.

## Changes in this continuation

PR #215 completes the read-only matcher evaluation left uncommitted by Claude.
This audit adds deterministic controls for a small value/overlay change, missing
snapshot, independent statements, zero-byte output, all six catalog/language
combinations, and mixed-source publication separation.

NC-111 exposed a real receipt bug: `_unproven` ignored `accepted_at`. Four new
cases (missing, NaN, before evaluation, after first write) failed before the fix
and passed after it. The fix requires a finite acceptance instant ordered
between evaluation and first write. This does not establish that the production
writer measured the acceptance instant or used the correct physical clock.

## Fresh verification

Local checks: Ruff check and format; mypy; seven import contracts; deptry.
Final offline run: **12,710 passed, 8 skipped, 0 failures**.
Mypy: 755 source files clean. Every NC reference resolves to a passing executed
test case in the JUnit output; this verifies execution, not the missing clauses
of partial/blocked controls. JUnit SHA-256:
`881e19c30f4964adecfbd3ab7b926fb772be194b8369afb7cd7baa340e38d569`.

Reproduce with `.venv/bin/pytest -n 4 -q -m 'not browser and not live and not container and not postgres and not simulator' --junitxml=/private/tmp/fakeyou-hardening-final-tests.xml`.
Browser, live, container, postgres and simulator markers are excluded locally. The simulator runs as cassette replay in GitHub CI.

Historical local counts quoted under each earlier PR are session evidence from
that PR description, not a new rerun of every historical SHA. CI below was
read from GitHub for each exact head. #206 is intentionally RED; #207–#215 have
successful Foundation and simulator runs. GitHub integration was read-only for
PR creation (403); the authorized GitHub CLI created #215.

External CodeRabbit review was not run because it sends code to an external
model/provider; the sprint prohibits such requests. Review here is local.

The original T6 RED evidence was also fetched directly from CI job
`132730485840` in [Foundation run 43687349993](https://github.com/maskentir/fakeyou/actions/runs/43687349993).
Both `large_figure_minor` and `small_figure_below_threshold` failed with
`AssertionError: assert ['completed'] == ['prevented']` at the RED head.
CI recorded 2 failed, 12,169 passed, 8 skipped and 89 deselected. This preserves
meaningful failure evidence independently of the earlier session narrative.

No historical fixture, dataset, cassette, ledger, scorecard, SLO, or production
configuration default was modified by this continuation. The historical-input
modification diff against `a79197b5` is empty; new evaluation labels/results
live in a new directory.

## Writer decision and unresolved production gates

The implementation in #209 calls its candidate “W2 with a deadline”. The local
fake passes scheduler, delayed-enqueue, withheld-output, and revalidation tests.
It is not accepted as a proven implementation of W2 for the production writer:

1. `accepted_at` is assigned from the engine evaluation instant before awaiting
   the writer; it does not measure when the executor actually accepts the frame.
2. PulseWriter checks its deadline before `_write`, then may open the device
   and enter a blocking `PaSimple.write`. It cannot guarantee withholding if
   the write exceeds the allowance after entry. The deadline also does not by
   itself guarantee the confirmation remains fresh at output.
3. A withheld write is re-evaluated by the engine, at most twice. A write that
   already happened is reported honestly as `fresh_at_write_unproven`; this is
   V4 telemetry, not permission to activate an unbounded writer.

W1 would also need an output bound after queue acceptance. Neither W1 nor W2
has a production proof here. **V6 remains unresolved and blocks activation.**
No timing limit or contract row was weakened.

Other unresolved gates:

- **NC-76:** `_state` calls synthesis preparation before playback can commit.
  A deterministic scene-change probe records the current text in the fake
  synthesizer, followed by a prevented current receipt with zero PCM and a
  historical replacement. The zero-PCM clause passes; the prohibition on
  synthesizing text whose eventual commit fails does not. This needs an
  implementable stage boundary or explicit contract resolution before a GO.
- **Y2 / NC-115:** RuntimeSpeech journals preparation as requested/finished,
  while PlaybackEngine starts actual synthesis lazily in a background task and
  may retry internally. There is no unique, budgeted ledger entry for every
  physical provider attempt. A `provider` mode string and preparation journal
  are not sufficient evidence of per-attempt budget authorization.
- **I14 / NC-118/119:** Reply-sink, renderer and malformed-proposal tests exclude
  model wording. An AST caller inventory is not a complete dynamic proof across
  direct, cue, lookahead, retry and previously prepared audio paths. Preparation
  can start before a publication binding exists; engine cue preparation has a
  separate synthesis path. The all-path acceptance controls remain incomplete.
- **D6 and digest stability:** native-resolution CPU budget and digest stability
  on a real Meet page are unmeasured. Real-page tests were not authorized.
- **Matcher coverage:** runtime answer-only EN coverage is 36.84%; no Indonesian
  cassette looks exist. Synthetic ID coverage (87.50%) is not a substitute.
- **Participant evidence:** consent, participant observers and real clock
  mapping are outside this offline sprint. Local PCM receipts are not audibility.

## Final offline readiness matrix

| Area | Offline evidence | Remaining gate |
| --- | --- | --- |
| T6 regression and digest/sample records | Intentional RED in #206; fix and deterministic tests in #207 | Native-page CPU/stability proof |
| Screen freshness and source/subject identity | Tables and confirmation/restart tests | Semantic identity and delivery delay remain simulated |
| Commit and local receipts | Race/delay controls; acceptance timestamp bug fixed | W1/W2/V6 production boundary unresolved |
| Abstention EN/ID | All three entries in both languages, one local-audio receipt each | Local audio availability/provider budget |
| Matcher/renderers | Labelled EN/ID controls; runtime-rendered statements | Low answer-only coverage; ID cassette gap; NC-76 |
| Historical fallback | Conservative 30 s limit, subject changes, provenance and receipt links | Dynamic authorized synthesis availability |
| Synthesis gate | Unavailable by default, silence journaled | Actual provider attempt/retry ledger and all-path proof |
| Negative controls | 94 covered / 24 partial / 1 blocked; 119 accounted for | Partial/blocked rows below must close before acceptance |
| Production/Phase 5B | OFF / NO-GO | Separate review and authorization; no simulation-based readiness claim |

## Invariant audit

“Offline” means evidence from deterministic fakes, not a production guarantee.

| Invariant | Evidence / limit |
| --- | --- |
| I1 | Limits remain 5.0 s / 0.25 s / 0.05 s; boundary tests |
| I2 | Analyzed capture/sample/snapshot are retained through later samples; immutable provenance tests |
| I3 | Confirmation has a separate identity/time; source and subject checks remain separate |
| I4 | Local gate/revalidator/renderer/fallback implementations use no additional vision/LLM request; exhaustive all-path count assertions partial (NC-104) |
| I5 | Missing/ambiguous support is unknown; labelled matcher tests |
| I6 | Existing aggregation/invalid-over-unknown decision tests remain in the offline suite |
| I7 | Commit fake scheduler and post-commit tests; production writer acceptance proof unresolved |
| I8 | Prevented/failed/zero-byte/cancelled receipts do not count as speech |
| I9 | Both production audio paths default to no context; source-wiring assertion |
| I10 | Runtime assigns scopes; model scope fields rejected |
| I11 | Main visual reply paths speak renderer/catalog text; no raw historical-prefix bypass |
| I12 | Receipt outcomes carry local-audio truth; gate/acceptance alone do not count |
| I13 | Runtime synthesis unavailable by default; Y2 physical-attempt ledger incomplete |
| I14 | Main reply path proven; all-path dynamic controls partial (NC-118/119) |
| I15 | Late writes marked unproven; production V2/V6 unresolved |

## Draft stack, exact refs and CI

Each PR is independently scoped against its immediate predecessor. None is
marked ready. CI is evaluated at the head listed here.

### [#206](https://github.com/maskentir/fakeyou/pull/206) — test(publication): reproduce T6 one-digit change published as unchanged (hardening 1/11, RED)

Base `main`: `a79197b5cf20d4235861b46edeb20165f4112bed`.
Head `test/t6-one-digit-regression`: `44b46085828bd369e39655d70d4aaa9b05f2ef43`.

Scope/contract rows: T6, NC-01/02; test-only intentional RED.

Remaining failures/downstream risks: Do not merge alone; expected two T6 failures.

CI: [Foundation checks](https://github.com/maskentir/fakeyou/actions/runs/43687349993): failure, [Meeting simulator](https://github.com/maskentir/fakeyou/actions/runs/43687349842): success.

Recorded test evidence:

Local, offline, at `44b4608`: 2 failed, 12,064 passed, 113 skipped. Both failures are `test_claim_about_a_figure_is_not_published_after_one_digit_changed`:

```
E  AssertionError: assert ['completed'] == ['prevented']
```

The slide changed from $1.2M to $1.3M between the look and the write; the sentence "Revenue is $1.2M." was written to the local output and counted as spoken. The control with no change publishes, so the failure is the change going unnoticed, not the rig.


### [#207](https://github.com/maskentir/fakeyou/pull/207) — feat(perception): digest records and confirmation samples, T6 fix (hardening 2/11)

Base `test/t6-one-digit-regression`: `44b46085828bd369e39655d70d4aaa9b05f2ef43`.
Head `feat/content-digest-samples`: `3f50b4f95d4927a6816fd7f55120ea8cd60af92a`.

Scope/contract rows: D1–D9, digest/sample types; T6 fix.

Remaining failures/downstream risks: Native-page hashing CPU budget and real Meet pixel stability unmeasured. Sensor browser tests excluded locally.

CI: [Foundation checks](https://github.com/maskentir/fakeyou/actions/runs/43696012435): success, [Meeting simulator](https://github.com/maskentir/fakeyou/actions/runs/43696012473): success.

Recorded test evidence:

Local, offline: 12,153 passed, 113 skipped. New tests: `test_content_digest.py` (10), `test_samples_ledger.py` (9), `test_revalidation_match.py` (17), `test_sensors_samples.py` (11), `test_publication_t6_regression.py` now 10 passing.

Local headless browser on a synthetic canvas stream served from a locally fulfilled origin (`pytest -m browser tests/test_sensors_browser.py`, 4 passed, not part of CI): every sample of a static canvas carries the same pixel digest with advancing media timestamps; a one-pixel change below the thumbnail threshold changes the digest while the timeline keeps one segment. A first run on `about:blank` returned `hash_failed` for every sample because `crypto.subtle` needs a secure context; the page fails closed there.

This is a canvas stream with no video codec. It says nothing about digest stability of a real Meet screen share.


### [#208](https://github.com/maskentir/fakeyou/pull/208) — feat(publication): screen freshness via exact confirmation samples (hardening 3/11)

Base `feat/content-digest-samples`: `3f50b4f95d4927a6816fd7f55120ea8cd60af92a`.
Head `feat/screen-freshness`: `cfc130680fc0ca653c3b796406a5a97d05ef2dbe`.

Scope/contract rows: S1–S18, R1–R5; exact screen freshness.

Remaining failures/downstream risks: Confirmation is content evidence only; source/subject identity depends on #211.

CI: [Foundation checks](https://github.com/maskentir/fakeyou/actions/runs/43696661002): success, [Meeting simulator](https://github.com/maskentir/fakeyou/actions/runs/43696661003): success.

Recorded test evidence:

Local, offline: 12,194 passed, 113 skipped. `tests/test_publication_freshness.py` (41): every row S1 to S14, S16, S18 as a parametrized table, capture and confirmation ages either side of 5.0 s inside the 0.05 s uncertainty (NC-05, NC-06), decision basis, immutability of the analyzed capture (NC-105), and end to end through the real engine: a slide 5.5 s, 30 s, and 300 s old with a fresh exact sample is published; with a stale exact sample it is `confirmation_too_old`; a 5.2 s reply with a fresh exact sample is published. S15, S16, S17 at the revalidator in `tests/test_revalidation_match.py` (20).

Three existing expectations changed to the contract's rows: the two `test_finding_*` screen tests are renamed (still prevented without a confirmation sample), `show_budget` is `scene_changed` instead of `subject_changed`, and a stopped share is `invalid: scene_changed` instead of `unknown: scene_continuity_unknown`.


### [#209](https://github.com/maskentir/fakeyou/pull/209) — feat(audio): commit evaluation and bounded first write (hardening 4/11)

Base `feat/screen-freshness`: `cfc130680fc0ca653c3b796406a5a97d05ef2dbe`.
Head `feat/commit-boundary`: `3d8d2079c3c12c758a47552993af3bc792331c4a`.

Scope/contract rows: R6–R24, W/V, I7/I12/I15; provisional W2 and receipts.

Remaining failures/downstream risks: V6 BLOCKED: engine-assigned acceptance is not measured executor acceptance; blocking device writes cannot be prevented after entry.

CI: [Foundation checks](https://github.com/maskentir/fakeyou/actions/runs/43697141710): success, [Meeting simulator](https://github.com/maskentir/fakeyou/actions/runs/43697141569): success.

Recorded test evidence:

Local, offline: 12,237 passed, 113 skipped. `tests/test_commit_boundary.py` (25) through the real engine and actor: non-exact sample during and after the preliminary gate (NC-38 to NC-40, NC-42), exact sample recorded on the committed decision (NC-41), confirmation fresh at the preliminary decision and old at the commit (NC-12), freeze then change then thaw (NC-45), no suspension between evaluation and handoff proven with a `call_soon` probe (NC-46), post-commit sample before the first write completes and captured before or after the commit (NC-43, NC-44, NC-53), change after the first write (NC-47), cancel before the first write completes (NC-48, NC-52, NC-112), failing first write (NC-49), withheld twice (NC-107), withheld then published (NC-108), scene changed during the delay (NC-109, NC-51), late write that could not be prevented (NC-110), missing or inconsistent instants (NC-111). `tests/test_pulse_writer.py` (+2): a write past its deadline never reaches the stream.

W1 versus W2: W1 needs a synchronous accept on every writer, and an accept into a queue is still not a write, so it would need the same deadline. W2 with a deadline changes one optional protocol method and keeps the wait where it already is.


### [#210](https://github.com/maskentir/fakeyou/pull/210) — feat(conversation): abstention catalog and substitution (hardening 5/11)

Base `feat/commit-boundary`: `3d8d2079c3c12c758a47552993af3bc792331c4a`.
Head `feat/abstention-catalog`: `a6cb84f23a29ac2edefee493fe157a9ed4157334`.

Scope/contract rows: A1–A15; versioned catalog and substitution.

Remaining failures/downstream risks: Catalog audio has no pre-existing-audio cache; synthesis availability is separate.

CI: [Foundation checks](https://github.com/maskentir/fakeyou/actions/runs/43698836841): success, [Meeting simulator](https://github.com/maskentir/fakeyou/actions/runs/43698836966): success.

Recorded test evidence:

Local, offline: 12,307 passed, 113 skipped. `tests/test_abstention_catalog.py` (36): catalog content in both languages, selection table, spacing and case accepted (A9), trailing factual clause, one changed word, a factual sentence, and a negative current claim refused (A8, A10), unknown version, language, or entry refused (A15), scope mismatch refused, the engine refusing to bind text whose digest differs, and through the real engine and actor in both languages: insufficient look with an inspected frame speaks AB2 with the failed look as evidence and no reply-model call; a look that inspected no frame speaks AB3; both counted once with one receipt carrying local audio (NC-54, NC-55). The readiness-sprint finding test for the abstaining look now asserts AB2 is spoken.


### [#211](https://github.com/maskentir/fakeyou/pull/211) — feat(publication): content, source and subject identity (hardening 6/11)

Base `feat/abstention-catalog`: `a6cb84f23a29ac2edefee493fe157a9ed4157334`.
Head `feat/claim-identity`: `4d2dda0d3ef1adeccf4df58a007276b292c06469`.

Scope/contract rows: N1–N4, C1–C10, P1–P5; source/subject identity.

Remaining failures/downstream risks: Semantic participant identity is supplied by deterministic mocks, not independent observation.

CI: [Foundation checks](https://github.com/maskentir/fakeyou/actions/runs/43699163581): success, [Meeting simulator](https://github.com/maskentir/fakeyou/actions/runs/43699163577): success.

Recorded test evidence:

Local, offline: 12,346 passed, 113 skipped. `tests/test_publication_identity.py` (34): the screen identity table by dependence, the camera table including identical frames from a replaced stream and from another participant (NC-26, NC-27), revalidator detection of another stream, another presenter, returned content, a dropped versus a stale camera, and end to end: a share restarted with the identical slide is `source_identity_changed` (NC-22, NC-25), another slide is `scene_changed` (NC-21), a fresh camera claim with no later sample is published. Two existing expectations moved to the contract's rows: a dropped camera is `invalid: scene_changed` (was `unknown`).


### [#212](https://github.com/maskentir/fakeyou/pull/212) — feat(foundation): structured claims and support matcher (hardening 7/11)

Base `feat/claim-identity`: `4d2dda0d3ef1adeccf4df58a007276b292c06469`.
Head `feat/claim-support`: `0c4b9af826370ff6f72aa60ec33450c04a3f35f5`.

Scope/contract rows: L1–L11; claim proposals and deterministic support.

Remaining failures/downstream risks: Textual relation matching is conservative; see answer-only low coverage in #215.

CI: [Foundation checks](https://github.com/maskentir/fakeyou/actions/runs/43699523384): success, [Meeting simulator](https://github.com/maskentir/fakeyou/actions/runs/43699523372): success.

Recorded test evidence:

Local, offline: 12,461 passed, 113 skipped. `tests/fixtures/claim-support-cases.json`: 64 labelled cases (38 English, 26 Indonesian) covering L1 and L4 to L9 in both languages, including "Costs $1.2M" for a revenue claim (NC-94), not approved and pending approval (NC-95, NC-96), a value inside a longer number or without its unit (NC-93), two figures in one clause (NC-99), coordinated roles, contrast, and a former value. `tests/test_claims_support.py` (98) runs every case, checks that each supported claim's spans are literal slices of the answer inside one clause, the proposal parser, and L2, L3, L9 at the evidence level (NC-35, NC-98).

This is a matcher result on a hand-labelled set written alongside the matcher. It is not a measurement on real look answers; that is 10/11.


### [#213](https://github.com/maskentir/fakeyou/pull/213) — feat(conversation): render-only visual speech (hardening 8/11)

Base `feat/claim-support`: `0c4b9af826370ff6f72aa60ec33450c04a3f35f5`.
Head `feat/visual-renderer`: `dad9bf26ec94138e746dc0e6e2b60f2315ad2f6a`.

Scope/contract rows: M1–M14, G1–G8, I10/I11/I14; runtime renderer.

Remaining failures/downstream risks: NC-76 synthesis-before-commit mismatch; cross-path NC-118/119 evidence incomplete.

CI: [Foundation checks](https://github.com/maskentir/fakeyou/actions/runs/43700457436): success, [Meeting simulator](https://github.com/maskentir/fakeyou/actions/runs/43700457405): success.

Recorded test evidence:

Local, offline: 12,608 passed, 113 skipped. `tests/test_rendering_patterns.py` (77): approved wording and equal placeholders in both languages, every supported labelled case rendered in both tenses only from spans of the answer and re-rendered byte for byte (NC-77), value fields carrying a second sentence, a marker, or no value refused (NC-68, NC-69, NC-72), every listed marker in both languages, both length limits and NFC (NC-70), negative polarity (NC-73), unknown language (NC-75), camera current refused. `tests/test_visual_speech.py` (30), through the real actor and engine with the synthesizer fake's inputs asserted: "and growing fast", "definitely", "right now", an unsupported negation, an undeclared approval, and a sentence imitating the historical pattern never reach the synthesizer (NC-63 to NC-66, NC-82); undeclared and neutral sentences yield one abstention (NC-79, NC-80, NC-97); unsupported, ambiguous, negated, and wrong-kind claims are never spoken; a model-declared scope voids the proposal (NC-60, NC-100); duplicates spoken once (NC-78); two claims are two statements; the engine refuses a model-written current binding. An AST test pins the four call sites in `conversation/` that reach `speaker.prepare`.

The streamed reply path is covered at the component level (`WithheldReplies`), not end to end; the rig drives the whole-reply path.


### [#214](https://github.com/maskentir/fakeyou/pull/214) — feat(conversation): historical fallback and synthesis gate (hardening 9/11)

Base `feat/visual-renderer`: `dad9bf26ec94138e746dc0e6e2b60f2315ad2f6a`.
Head `feat/historical-fallback`: `1ada18c07ef40f08c1ba51b7a13c9d8f9ddd1759`.

Scope/contract rows: H1–H6, F1–F13, Y1–Y6, I13; historical and synthesis gates.

Remaining failures/downstream risks: Y2 per-provider-attempt retry/budget ledger is not implemented. Runtime preparation journal is insufficient for it.

CI: [Foundation checks](https://github.com/maskentir/fakeyou/actions/runs/43701332155): success, [Meeting simulator](https://github.com/maskentir/fakeyou/actions/runs/43701332189): success.

Recorded test evidence:

Local, offline: 12,671 passed, 113 skipped. `tests/test_visual_speech_fallback.py` (42): the 30 s limit either side with uncertainty, an unproduced look, an unmapped capture, a subject change, and through the real actor and engine: statements prevented at 8 s, 12 s, and 29.9 s fall back to the historical pattern and at 31 s to AB1 (NC-81, NC-84); a restarted share with a fresh exact sample and a 40 s old checked frame abstains (NC-85); an unmapped capture abstains; two unanswerable statements get one abstention (NC-61); one eligible and one unsupported claim give one historical statement and one abstention (NC-87); a camera claim past 30 s and a camera stream that changed subject abstain (NC-86); an interrupted statement is followed by silence; a model-written historical binding is `statement_not_rendered` (NC-83); historical gathers (NC-89 to NC-91); default synthesis is unavailable and the turn is silent with both statements journaled (NC-113, NC-117); nothing is synthesized ahead (NC-116); requested, finished, failed, cancelled, and never-published requests are in the journal (NC-115); the speech registry is patched to fail and is never called (NC-114). Tests of earlier PRs that asserted silence after a prevented statement now assert the fallback.

Every synthesis in these tests is the fake synthesizer. No speech provider request was made.


### [#215](https://github.com/maskentir/fakeyou/pull/215) — test(evals): offline support matcher evaluation (hardening 10/11)

Base `feat/historical-fallback`: `1ada18c07ef40f08c1ba51b7a13c9d8f9ddd1759`.
Head `eval/support-matcher`: `dfb775b7f48b1e18fdb0337b21a87dc020ad37f5`.

Scope/contract rows: Section 13, locked decision 15; read-only matcher evaluation.

Remaining failures/downstream risks: No Indonesian cassette samples; synthetic rates are separate. Zero false support on a small labelled set is not a safety proof.

CI: [Foundation checks](https://github.com/maskentir/fakeyou/actions/runs/43702880642): success, [Meeting simulator](https://github.com/maskentir/fakeyou/actions/runs/43702880664): success.

Recorded test evidence:

Fresh continuation run: 9 evaluation tests. 12,689 passed, 8 skipped in offline suite (browser/live/container/postgres/simulator excluded); Ruff check/format, mypy (754 files), 7 import contracts and deptry passed.

## NC-01 through NC-119 evidence index

Covered = an offline assertion is available and executed; partial = only the
specified subset is established; blocked = observed mismatch. Test names may
expand to multiple parameter cases. Referencing a passing test does not make
every clause of a partial control pass. Machine-readable companion:
`2026-11-15-hardening-audit.json`.

| Control | Status | Executed test / remaining gap |
| --- | --- | --- |
| NC-01 | covered | `tests/test_publication_t6_regression.py::test_claim_about_a_figure_is_not_published_after_one_digit_changed` |
| NC-02 | covered | `tests/test_publication_t6_regression.py::test_claim_about_a_figure_is_not_published_after_one_digit_changed` |
| NC-03 | covered | `tests/test_publication_negative_controls.py::test_small_content_or_overlay_change_never_confirms_the_checked_figure` Synthetic one-digit table value and overlay on the claimed text. Real-page/native pixel sampling remains outside the authorized tests. |
| NC-04 | covered | `tests/test_publication_negative_controls.py::test_small_content_or_overlay_change_never_confirms_the_checked_figure` Synthetic one-digit table value and overlay on the claimed text. Real-page/native pixel sampling remains outside the authorized tests. |
| NC-05 | covered | `tests/test_publication_freshness.py::test_capture_age_near_the_limit_is_never_valid_inside_its_uncertainty` |
| NC-06 | covered | `tests/test_publication_freshness.py::test_confirmation_age_near_the_limit_is_never_valid_inside_its_uncertainty` |
| NC-07 | covered | `tests/test_publication_freshness.py::test_screen_content_decision_table` |
| NC-08 | covered | `tests/test_publication_freshness.py::test_screen_content_decision_table` |
| NC-09 | covered | `tests/test_publication_freshness.py::test_screen_content_decision_table` |
| NC-10 | covered | `tests/test_publication_freshness.py::test_screen_content_decision_table` |
| NC-11 | covered | `tests/test_publication_freshness.py::test_static_slide_whose_last_exact_sample_is_stale_is_prevented` |
| NC-12 | covered | `tests/test_commit_boundary.py::test_confirmation_fresh_at_the_preliminary_decision_and_old_at_the_commit` |
| NC-13 | covered | `tests/test_publication_activation.py::test_static_slide_without_a_confirmation_sample_is_prevented` |
| NC-14 | covered | `tests/test_revalidation_match.py::test_camera_heartbeat_without_a_record_is_unknown` |
| NC-15 | covered | `tests/test_samples_ledger.py::test_repeated_media_timestamp_is_not_a_new_capture` |
| NC-16 | covered | `tests/test_samples_ledger.py::test_lower_or_equal_sequence_is_discarded` |
| NC-17 | covered | `tests/test_samples_ledger.py::test_higher_sequence_with_an_earlier_capture_time_is_marked_inconsistent` |
| NC-18 | covered | `tests/test_samples_ledger.py::test_skipped_hash_is_stored_without_reusing_the_previous_digest` |
| NC-19 | covered | `tests/test_content_digest.py::test_records_that_are_not_comparable_say_nothing_about_content` |
| NC-20 | covered | `tests/test_content_digest.py::test_records_that_are_not_comparable_say_nothing_about_content` |
| NC-21 | covered | `tests/test_publication_identity.py::test_another_slide_is_a_changed_scene_end_to_end` |
| NC-22 | covered | `tests/test_publication_identity.py::test_restarted_share_with_the_identical_slide_does_not_prove_the_source` |
| NC-23 | partial | `tests/test_publication_identity.py::test_same_content_shown_by_another_presenter_is_a_changed_source` Presenter-change source attribution is tested; the state gate is separate. No one combined pixel-identical-presenter-transition test covers all clauses. |
| NC-24 | covered | `tests/test_publication_identity.py::test_content_that_returned_after_another_slide_is_a_changed_source` |
| NC-25 | covered | `tests/test_publication_identity.py::test_screen_identity_table` |
| NC-26 | covered | `tests/test_publication_identity.py::test_replaced_camera_stream_of_the_same_participant_is_not_the_same_scene` |
| NC-27 | covered | `tests/test_publication_identity.py::test_camera_stream_that_now_belongs_to_another_participant_is_a_changed_subject` |
| NC-28 | partial | `tests/test_publication_identity.py::test_camera_identity_table` Similar/distinct camera outcomes are deterministic table inputs; the exact pose/second-person image sequence is not a dedicated fixture. |
| NC-29 | partial | `tests/test_publication_identity.py::test_camera_identity_table` Similar/distinct camera outcomes are deterministic table inputs; the exact pose/second-person image sequence is not a dedicated fixture. |
| NC-30 | partial | `tests/test_publication_identity.py::test_camera_identity_table` Similar/distinct camera outcomes are deterministic table inputs; the exact pose/second-person image sequence is not a dedicated fixture. |
| NC-31 | partial | `tests/test_publication_identity.py::test_camera_identity_table` Exact camera and historical-only renderer are tested separately; no combined frozen-camera advancing-media-timestamps end-to-end case. |
| NC-32 | partial | `tests/test_revalidation_match.py::test_repeated_frame_does_not_become_the_confirmation` Repeated-frame filtering is tested; frozen camera past 5 s is not a dedicated combined case. |
| NC-33 | covered | `tests/test_publication_identity.py::test_camera_identity_table` |
| NC-34 | covered | `tests/test_publication_freshness.py::test_screen_content_decision_table` |
| NC-35 | covered | `tests/test_claims_support.py::test_claim_without_a_look_of_its_kind_is_missing_evidence` |
| NC-36 | covered | `tests/test_abstention_catalog.py::test_look_that_inspected_no_frame_is_answered_with_the_uninspected_entry` |
| NC-37 | covered | `tests/test_publication_negative_controls.py::test_required_snapshot_missing_never_becomes_valid` |
| NC-38 | covered | `tests/test_commit_boundary.py::test_non_exact_sample_before_the_commit_hands_nothing_to_the_writer` |
| NC-39 | covered | `tests/test_commit_boundary.py::test_non_exact_sample_before_the_commit_hands_nothing_to_the_writer` |
| NC-40 | covered | `tests/test_commit_boundary.py::test_non_exact_sample_before_the_commit_hands_nothing_to_the_writer` |
| NC-41 | covered | `tests/test_commit_boundary.py::test_exact_sample_before_the_commit_is_recorded_on_the_committed_decision` |
| NC-42 | covered | `tests/test_commit_boundary.py::test_non_exact_sample_before_the_commit_hands_nothing_to_the_writer` |
| NC-43 | covered | `tests/test_commit_boundary.py::test_sample_after_the_commit_is_evidence_and_leaves_the_decision_alone` |
| NC-44 | covered | `tests/test_commit_boundary.py::test_sample_after_the_commit_is_evidence_and_leaves_the_decision_alone` |
| NC-45 | covered | `tests/test_commit_boundary.py::test_freeze_after_the_preliminary_decision_commits_after_the_thaw` |
| NC-46 | partial | `tests/test_commit_boundary.py::test_no_suspension_point_lies_between_the_final_evaluation_and_the_handoff` call_soon probe proves immediate entry to the fake writer coroutine only. Production executor acceptance is not measured; accepted_at is assigned by the engine before awaiting. |
| NC-47 | covered | `tests/test_commit_boundary.py::test_change_after_the_first_write_does_not_rewrite_the_decision` |
| NC-48 | covered | `tests/test_commit_boundary.py::test_committed_attempt_cancelled_before_its_first_write_is_not_spoken` |
| NC-49 | covered | `tests/test_commit_boundary.py::test_committed_attempt_whose_first_write_fails_is_not_spoken` |
| NC-50 | partial | `tests/test_publication_negative_controls.py::test_zero_byte_clip_is_not_counted_or_recorded_as_speech` Zero-byte output is unspoken; no first frame means the current implementation never reaches a commit, so the exact committed-valid/zero-byte combination is not established. |
| NC-51 | partial | `tests/test_commit_boundary.py::test_withheld_write_whose_scene_changed_during_the_delay_is_prevented` Delay and changed scene tested, but readiness/back-pressure preceding final validation is not demonstrated for production W2. |
| NC-52 | covered | `tests/test_commit_boundary.py::test_committed_attempt_cancelled_before_its_first_write_is_not_spoken` |
| NC-53 | covered | `tests/test_commit_boundary.py::test_sample_after_the_commit_is_evidence_and_leaves_the_decision_alone` |
| NC-54 | covered | `tests/test_publication_negative_controls.py::test_each_catalog_entry_has_exactly_one_audio_receipt_in_each_language` |
| NC-55 | covered | `tests/test_abstention_catalog.py::test_look_that_inspected_no_frame_is_answered_with_the_uninspected_entry` |
| NC-56 | covered | `tests/test_abstention_catalog.py::test_text_that_is_not_exactly_an_entry_is_refused` |
| NC-57 | covered | `tests/test_abstention_catalog.py::test_insufficient_look_is_answered_with_the_inspected_frame_entry` |
| NC-58 | covered | `tests/test_abstention_catalog.py::test_insufficient_look_is_answered_with_the_inspected_frame_entry` |
| NC-59 | covered | `tests/test_abstention_catalog.py::test_text_that_is_not_exactly_an_entry_is_refused` |
| NC-60 | covered | `tests/test_visual_speech.py::test_model_declared_scope_or_extra_field_voids_the_proposal` |
| NC-61 | covered | `tests/test_visual_speech_fallback.py::test_two_prevented_statements_that_cannot_fall_back_get_one_abstention` |
| NC-62 | covered | `tests/test_abstention_catalog.py::test_reference_to_a_version_language_or_entry_that_does_not_exist_is_refused` |
| NC-63 | covered | `tests/test_visual_speech.py::test_extra_assertion_in_the_model_sentence_never_reaches_the_synthesizer` |
| NC-64 | covered | `tests/test_visual_speech.py::test_extra_assertion_in_the_model_sentence_never_reaches_the_synthesizer` |
| NC-65 | covered | `tests/test_visual_speech.py::test_extra_assertion_in_the_model_sentence_never_reaches_the_synthesizer` |
| NC-66 | partial | `tests/test_visual_speech.py::test_claim_that_is_not_supported_or_not_renderable_is_never_spoken` Polarity matching and raw extra-negation exclusion are tested; the exact Costs approved/negative proposal case is not separately asserted. |
| NC-67 | covered | `tests/test_visual_speech.py::test_extra_assertion_in_the_model_sentence_never_reaches_the_synthesizer` |
| NC-68 | covered | `tests/test_rendering_patterns.py::test_value_field_that_carries_more_than_a_value_is_not_rendered` |
| NC-69 | covered | `tests/test_rendering_patterns.py::test_every_listed_marker_blocks_a_field_in_its_language` |
| NC-70 | covered | `tests/test_rendering_patterns.py::test_entity_field_with_a_marker_punctuation_or_excess_length_is_not_rendered` |
| NC-71 | partial | `tests/test_rendering_patterns.py::test_missing_field_span_is_not_rendered` Missing span tested. Forged/cross-clause span provenance is not independently validated by render; it trusts Support supplied by the matcher. |
| NC-72 | covered | `tests/test_rendering_patterns.py::test_value_field_that_carries_more_than_a_value_is_not_rendered` |
| NC-73 | partial | `tests/test_rendering_patterns.py::test_negative_claim_has_no_pattern` Supported negative is rejected by renderer. Conditional proposals are rejected at parsing, with no dedicated conditional rendering reason assertion. |
| NC-74 | partial | `tests/test_claims_support.py::test_malformed_or_extended_proposal_yields_no_claims` Unsupported shape fails proposal parsing; required claim_render_unsupported reason and AB1 path are not a dedicated case. |
| NC-75 | covered | `tests/test_visual_speech.py::test_turn_language_without_a_pattern_abstains` |
| NC-76 | blocked | `tests/test_visual_speech_fallback.py::test_synthesis_that_is_prevented_from_publication_is_still_in_the_ledger` Confirmed mismatch: current rendering is synthesized before the final commit and can then be prevented. Zero PCM passes; the zero-current-synthesis clause fails. No contract weakening or production readiness claim. |
| NC-77 | covered | `tests/test_rendering_patterns.py::test_supported_labelled_case_renders_only_from_spans_of_the_answer` |
| NC-78 | covered | `tests/test_visual_speech.py::test_same_claim_declared_twice_is_spoken_once` |
| NC-79 | covered | `tests/test_visual_speech.py::test_visual_turn_without_declared_claims_speaks_one_abstention_only` |
| NC-80 | covered | `tests/test_visual_speech.py::test_visual_turn_without_declared_claims_speaks_one_abstention_only` |
| NC-81 | covered | `tests/test_visual_speech_fallback.py::test_statement_prevented_by_age_falls_back_by_the_age_of_the_checked_frame` |
| NC-82 | covered | `tests/test_visual_speech.py::test_extra_assertion_in_the_model_sentence_never_reaches_the_synthesizer` |
| NC-83 | covered | `tests/test_visual_speech_fallback.py::test_model_written_historical_binding_is_refused_under_a_render_only_context` |
| NC-84 | covered | `tests/test_visual_speech_fallback.py::test_historical_statement_is_limited_to_thirty_seconds_from_the_analyzed_capture` |
| NC-85 | covered | `tests/test_visual_speech_fallback.py::test_thirty_seconds_are_measured_from_the_checked_frame_not_the_confirmation` |
| NC-86 | covered | `tests/test_visual_speech_fallback.py::test_camera_stream_that_changed_subject_gets_the_abstention` |
| NC-87 | covered | `tests/test_visual_speech_fallback.py::test_one_claim_falls_back_and_one_unsupported_claim_gets_the_abstention` |
| NC-88 | partial | `tests/test_visual_speech_fallback.py::test_historical_statement_links_the_prevented_publication_and_shares_its_evidence` ID, evidence and replacement linkage covered; no single test asserts exactly unchanged vision/LLM call counts after gather. |
| NC-89 | covered | `tests/test_visual_speech_fallback.py::test_historical_gather_is_rendered_historically_without_a_freshness_check` |
| NC-90 | covered | `tests/test_visual_speech_fallback.py::test_historical_gather_with_an_unsupported_claim_abstains` |
| NC-91 | covered | `tests/test_visual_speech_fallback.py::test_historical_gather_without_a_mapped_capture_abstains` |
| NC-92 | partial | `tests/test_visual_speech.py::test_two_claims_are_two_statements_decided_separately` Two screen claims are independent; screen valid plus camera unknown is not an explicit mixed-source end-to-end assertion. |
| NC-93 | covered | `tests/test_claims_support.py::test_labelled_support_case` |
| NC-94 | covered | `tests/test_claims_support.py::test_labelled_support_case` |
| NC-95 | covered | `tests/test_claims_support.py::test_labelled_support_case` |
| NC-96 | covered | `tests/test_claims_support.py::test_labelled_support_case` |
| NC-97 | covered | `tests/test_visual_speech.py::test_visual_turn_without_declared_claims_speaks_one_abstention_only` |
| NC-98 | covered | `tests/test_claims_support.py::test_claim_declared_against_evidence_of_another_source_is_a_mismatch` |
| NC-99 | covered | `tests/test_claims_support.py::test_labelled_support_case` |
| NC-100 | covered | `tests/test_visual_speech.py::test_model_declared_scope_or_extra_field_voids_the_proposal` |
| NC-101 | covered | `tests/test_publication_negative_controls.py::test_second_independent_statement_can_publish_after_first_is_prevented` |
| NC-102 | covered | `tests/test_publication_activation.py::test_fresh_supported_screen_claim_is_published_and_counted` Entire activation module is run, not merely this representative case; historical behavior changed as specified. |
| NC-103 | covered | `tests/test_publication_activation.py::test_no_production_module_composes_the_opt_in_gate` |
| NC-104 | partial | `tests/test_visual_speech_fallback.py::test_real_speech_provider_is_never_constructed_in_a_fallback_turn` Local fakes and provider-registry refusal cover fallback. Universal vision/LLM call-count assertions across every gate/revalidator/renderer path are not exhaustive. |
| NC-105 | covered | `tests/test_revalidation_match.py::test_analyzed_record_is_not_changed_by_later_samples` |
| NC-106 | covered | `tests/test_visual_speech.py::test_turn_without_visual_needs_speaks_the_model_text_as_today` |
| NC-107 | covered | `tests/test_commit_boundary.py::test_first_write_delayed_past_the_validation_window_is_withheld` |
| NC-108 | partial | `tests/test_commit_boundary.py::test_withheld_write_is_re_evaluated_and_published_when_still_valid` Evaluations count and later timestamp are receipted; individual evaluations live in the journal. No production atomic output revalidation proof. |
| NC-109 | covered | `tests/test_commit_boundary.py::test_withheld_write_whose_scene_changed_during_the_delay_is_prevented` |
| NC-110 | covered | `tests/test_commit_boundary.py::test_late_write_that_could_not_be_prevented_is_reported_and_marked_unproven` |
| NC-111 | partial | `tests/test_commit_boundary.py::test_missing_or_out_of_order_writer_acceptance_cannot_prove_freshness` Missing/nonfinite/out-of-order acceptance now fails closed (4 RED then GREEN). Instants from different physical clocks are not individually tagged; production receipt timing remains unproven. |
| NC-112 | covered | `tests/test_commit_boundary.py::test_committed_attempt_cancelled_before_its_first_write_is_not_spoken` |
| NC-113 | covered | `tests/test_visual_speech_fallback.py::test_without_authorized_synthesis_a_visual_turn_is_silent_and_journaled` |
| NC-114 | partial | `tests/test_visual_speech_fallback.py::test_real_speech_provider_is_never_constructed_in_a_fallback_turn` Targeted fallback registry refusal and fake synthesizer are proven. Whole legacy suite may construct mocked real client classes; no universal constructor prohibition exists. |
| NC-115 | partial | `tests/test_visual_speech_fallback.py::test_every_runtime_synthesis_request_is_journaled_with_its_authorization` Requested/failed/cancelled/unpublished paths covered by related tests. Runtime prepare logs preparation, while actual engine synthesis is lazy and can retry internally; no per-provider-attempt budget ledger is implemented. |
| NC-116 | partial | `tests/test_visual_speech_fallback.py::test_nothing_is_synthesized_ahead_of_a_selected_statement` Runtime catalog has no prebuild path; engine.prepare_cue is a separate pre-synthesis API and lacks this authorization gate. |
| NC-117 | partial | `tests/test_visual_speech_fallback.py::test_without_authorized_synthesis_a_visual_turn_is_silent_and_journaled` Unavailable synthesis silence is tested. No existing-audio-only mode/catalog cache exists to execute the exact dynamic-historical-with-catalog-only case. |
| NC-118 | partial | `tests/test_visual_speech.py::test_synthesis_is_reached_from_the_conversation_only_through_two_guarded_funnels` AST caller inventory and reply tests do not exercise every direct/cue/lookahead/retry path. Playback prepare starts a background synthesis task before binding; engine APIs have no per-turn visual synthesis capability. |
| NC-119 | partial | `tests/test_turn_graph_draft.py::test_a_turn_that_needs_a_look_answers_afresh_and_drops_the_draft` Speculative text draft is dropped when evidence needs change. The test has no publication context and does not cover a pre-synthesized audio artifact crossing into a visual turn. |
