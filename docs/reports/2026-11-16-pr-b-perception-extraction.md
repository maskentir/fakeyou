# PR B: perception freshness and identity extraction report

Date: 2026-11-16. Branch `feat/perception-freshness-identity`, created from
post-#220 `main` `70986cefccf62f0e6c5606bd37c0246df4c75e5c` and rebased onto
`687d86e23aaf5cb357e8def4753dd378c4da0a4a` (docs-only archive #228).

PublicationContext **OFF**; Phase 5B **NO-GO**; V6 **NO-GO**; contract
[#205](https://github.com/maskentir/fakeyou/pull/205) unchanged. No provider
request, spend, real Meet page or audio activation. A `valid` pure decision is
not permission to synthesize, commit or write PCM.

## Inputs

| Input | Exact revision |
| --- | --- |
| Frozen plan | [`docs/plans/2026-11-16-pr-b-perception-extraction.md` at `44a5739e`](https://github.com/maskentir/fakeyou/blob/44a5739ea74ff2ebdd38ae54b768e36dc5fd593a/docs/plans/2026-11-16-pr-b-perception-extraction.md) |
| #208 freshness | `cfc130680fc0ca653c3b796406a5a97d05ef2dbe` |
| #211 identity | `4d2dda0d3ef1adeccf4df58a007276b292c06469` |
| Selected #216 lifecycle | `3e9eb0f1`, `cb3bd971`, `76f047bf` at head `62b58e2f89111fff2cfded8f96463b15f1e64006` |

Changes were copied by responsibility. No legacy commit was merged,
cherry-picked or rebased into this branch.

## Scope delivered

Eight production files, exactly the plan allowlist:
`assets/perception.js`, `foundation/content_digest.py`,
`foundation/publication.py`, and `perception/{context,revalidation,samples,sensors,timeline}.py`.

Compared with the historical stack head `62b58e2f`:

- `content_digest.py`, `context.py`, `revalidation.py` and `samples.py` are
  byte-identical to it.
- `sensors.py` differs deliberately, see findings 5 and 6.
- `publication.py` omits every #209/#210/#213/#214 hunk: no abstention import,
  no rendering or replacement fields, no commit/revision/sample callbacks on
  `PublicationContext`, no runtime synthesis mode and no historical decision.
  It also differs deliberately, see finding 4.
- `timeline.py` differs deliberately, see finding 1.
- `perception.js` differs deliberately, see finding 2.

Not imported: #209 writer and W2 claims, #210 abstention activation, #212
`CLAIMS:` parsing, #213/#214 publication and synthesis flows, #216 commits
`19ad3ff5`, `df421c86`, `be093039`, and any provider or settings change.

## Findings during extraction

1. **Identical later frame replaced the analyzed sample.** The historical
   timeline keyed its analyzed cache by content hash alone, so showing the same
   slide again overwrote the original analyzed capture. The cache is now keyed
   by hash and capture time and never overwrites. Control:
   `test_identical_later_frame_keeps_the_original_analyzed_sample`, RED on the
   historical timeline and GREEN here.
2. **The eye did not install outside a secure context.** The historical asset
   called `crypto.randomUUID()`, which is undefined on a non-secure page such
   as `about:blank`. The script aborted, the drain returned `null`, and two of
   the four synthetic browser controls failed (4 passed on `main`, 2 failed on
   the first port). The historical stack never ran the browser marker, locally
   or in CI, so this was not visible there. The page session now falls back to
   `crypto.getRandomValues`. Browser assertions are unchanged; the Node VM
   harness gained a context without `randomUUID`.
3. **Contract-driven expectation changes in existing tests.** These follow
   #205 and the historical #208/#211 edits; every case is still prevented and
   none becomes publishable.

| Test | Before | After | Basis |
| --- | --- | --- | --- |
| `test_publication_audit` lost camera, end to end | `prevented_unknown` | `prevented_invalid` (renamed) | NC-33, #211 |
| `test_publication_activation` camera went away | `unknown:scene_continuity_unknown` | `invalid:scene_changed` | NC-33, #211 |
| `test_publication_activation` another slide | `invalid:subject_changed` | `invalid:scene_changed` | NC-21, #208 |
| `test_publication_activation` share stopped | `unknown:scene_continuity_unknown` | `invalid:scene_changed` | #208 |
| `test_publication_activation` static slide older than 120 s | `unknown:capture_time_unmapped` | `invalid:capture_too_old` | capture mapped at ingestion, #208 |

## Findings from independent review of the first head

An independent read-only review of head `6180918c` found no production path
to a wrong `valid`. It confirmed four defects inherited from the historical
design; each was reproduced with a local probe before any change.

4. **Latent fail-open in the pure decision.** A screen `Validation` with an
   exact match, `unchanged=False` and the default `source_changed=False`
   returned `valid` for a source or undeclared claim; before this extraction
   `unchanged=False` was `invalid`. The production revalidator always sets
   `source_changed` in that case, so it was not reachable, but any other
   producer would fail open. `unchanged is False` now counts as a changed
   source: `unknown` for source and undeclared claims, content claims as in
   NC-22 to NC-24. Three rows added to `test_screen_identity_table`, RED first.
5. **A camera turned off and on inside one drain stayed blind.** The stop
   retired the sampler incarnation while the assignment list was unchanged,
   so the page sampler kept its retired generation and every later sample and
   frame was refused until reassignment or reload. The drain now restarts the
   page sampler of a stopped device that is still assigned, which yields a new
   generation. Control:
   `test_camera_turned_off_and_on_inside_one_drain_restarts_its_sampler`.
6. **A frame drained before its digest sample was dropped.** The asset
   produces the JPEG and the digest on independent asynchronous chains, so a
   drain can land between them. `main` showed such a frame; the first port
   dropped it as `retired_stream`, leaving a settled slide unseen. The frame
   now waits for exactly one more drain and is dropped as `sample_missing` if
   its sample still has not arrived. A frame whose sample was refused is still
   dropped at once as `retired_stream`, and a repeated sample cannot unbind
   the admitted one. Controls:
   `test_frame_drained_before_its_sample_is_shown_once_the_sample_arrives`,
   `test_frame_whose_sample_never_arrives_is_dropped_after_one_more_drain`,
   `test_repeated_sample_does_not_unbind_the_frame_of_the_admitted_one`.
7. **Not fixed here: a revisited slide stays `unknown` under a new presenter.**
   The source check compares the segment's first presenter with the latest
   one, not the presenter at binding time, so a source or undeclared claim on
   a slide first shown by someone else is `unknown: source_identity_changed`
   whenever a newer sample exists. This is over-conservative, never
   publishable, and only reachable through the publication gate, which is
   OFF. Tracked in [#225](https://github.com/maskentir/fakeyou/issues/225).

The review also listed lower-severity observations that are left as they
are: unused `SampleLedger.since`, confirmation bounds that ignore mapping
uncertainty within the 5 s rule, and several tests that assert less than
their names suggest.

`test_publication_audit.py` and `test_publication_activation.py` are outside
the plan's candidate test list; they are touched only for the rows above and
for binding the capture clock at ingestion in the shared `Meeting` fixture.

## Negative controls

| Control | Executed by |
| --- | --- |
| B-01 | `test_publication_t6_regression.py`; browser `test_one_pixel_change_below_the_thumbnail_threshold_changes_the_digest` |
| B-02 | `test_screen_content_decision_table`, `test_confirmation_never_changes_the_analyzed_capture`, `test_last_revalidation_cannot_refresh_first_capture_age` |
| B-03 | `test_capture_age_near_the_limit_is_never_valid_inside_its_uncertainty`, `test_confirmation_age_near_the_limit_is_never_valid_inside_its_uncertainty`, `test_threshold_crossing_uncertainty_is_unknown` |
| B-04 | `test_screen_content_decision_table`, `test_later_clock_exchange_never_remaps_an_originally_unmapped_capture`, `test_unmapped_capture_never_falls_back_to_the_observation_time` |
| B-05 | `test_skipped_hash_is_stored_without_reusing_the_previous_digest`, `test_repeated_media_timestamp_is_not_a_new_capture`, `test_lower_or_equal_sequence_is_discarded`, `test_higher_sequence_with_an_earlier_capture_time_is_marked_inconsistent`, `test_repeated_frame_does_not_become_the_confirmation` |
| B-06 | `test_static_slide_whose_last_exact_sample_is_stale_is_refused`, `test_stopped_share_is_reported_as_a_stopped_source` |
| B-07 | `test_screen_identity_table`, `test_restarted_share_with_the_identical_slide_does_not_prove_the_source`, `test_same_content_shown_by_another_presenter_is_a_changed_source`, `test_content_that_returned_after_another_slide_is_a_changed_source` |
| B-08 | `test_camera_identity_table`, `test_replaced_camera_stream_of_the_same_participant_is_not_the_same_scene`, `test_camera_stream_that_now_belongs_to_another_participant_is_a_changed_subject`, `test_dropped_camera_is_a_stopped_source_and_a_stale_one_is_not` |
| B-09 | `test_camera_identity_table`, `test_camera_frame_with_another_digest_is_similar_never_distinct` |
| B-10 | `test_restarted_stream_accepts_reset_sequence_and_media_timestamp`, `test_same_stream_id_new_sampler_generation_can_reset_counters`, `test_sampler_restart_resets_counters_without_binding_a_retired_frame` |
| B-11 | `test_retired_camera_sampler_cannot_be_relabelled_as_a_new_subject`, `test_late_retired_stream_cannot_replace_the_restarted_stream` |
| B-12 | `test_sampler_reassignment_and_old_completion_preserve_the_successor` (Node VM against the real asset) |
| B-13 | `test_new_page_epoch_accepts_reset_sampler_generation_and_counters`, `test_retired_page_cannot_replace_new_page_with_a_higher_generation`, `test_generation_regression_is_still_rejected_within_the_same_page` |
| B-14 | `test_new_batch_cannot_relabel_retired_page_parts` |
| B-15 | `test_drain_in_flight_cannot_cross_a_python_page_epoch`, `test_decode_in_flight_does_not_admit_the_rest_of_a_retired_batch` |
| B-16 | `test_reload_detected_by_the_drain_starts_a_new_page_and_reassigns_streams`, `test_new_epoch_does_not_reuse_the_previous_page_clock_exchange` |
| B-17 | `test_stream_owner_lookup_does_not_relabel_old_page_evidence`, `test_page_epoch_scopes_generation_and_rejects_old_page_records` |

## Local execution

macOS arm64, Python 3.12, `uv sync --locked --all-groups --offline`, four
workers, default marker exclusions (`not browser and not live and not container`).

| Run | Result |
| --- | --- |
| Focused files, first run | 319 passed, **1 failed** (`test_publication_audit` lost camera; finding 3) |
| Full offline suite before the asset fix | 12,281 passed, 113 skipped, 0 failed; JUnit SHA-256 `323219505f0724581ca3fc20d347580d84ece9eb77d4b18228950fefc7501ead` |
| Browser controls, first port | 2 passed, **2 failed** (finding 2); `main` baseline 4 passed |
| Browser controls, first head | 4 passed; JUnit SHA-256 `59bb756afc9e1a75bc5f21c2bcf86895fe0ad71283c77ef4ff109a0d5830030b` |
| Full offline suite, first head | 12,281 passed, 113 skipped, 0 failed; JUnit SHA-256 `8a9ccb68d2b71290f5b8c36d6e37d24a0e5b715c0adf4339d0bba8f28bc31dde` |
| Exact-head CI on first head `6180918c` | 12,386 passed, 8 skipped, 91 deselected; node, secrets and simulator replay passed |
| Review regression tests before the fixes | **6 failed** (findings 4 to 6) |
| Full offline suite after the review fixes | 12,288 passed, 113 skipped, 0 failed; JUnit SHA-256 `6baa2a0ff6ef78cdfcc9631d985158a444cfdb30c9a39d1399010a4f65423493` |
| Browser controls after the review fixes | 4 passed; JUnit SHA-256 `597095018ed9f8caa53c6ae1fa3fd73c6b94eb1bd95742695c219fb350f895f3` |
| Node VM harness | passed, Node 26.5.1 |
| Ruff check and format, mypy (743 files), import-linter (7 contracts), deptry | passed |

Browser controls ran against the cached browser binary under a sandbox profile
that denies all outbound network except localhost. No real Meet page was
opened. The early-commit case tracked in
[#223](https://github.com/maskentir/fakeyou/issues/223) passed in both full
runs; that is not evidence about its historical failure.

## Limits

- Real-page digest CPU, stability and participant evidence remain unmeasured.
- The JUnit files and logs are local artifacts identified by hash only.
- The historical 98/20/1 audit classification is not promoted by this
  extraction, and no historical report is edited.
