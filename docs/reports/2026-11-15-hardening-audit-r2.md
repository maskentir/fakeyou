# Phase 5A Hardening — review corrections, revision 2

**Offline acceptance remains incomplete. No mark-ready, merge or activation.**
PublicationContext OFF; Phase 5B NO-GO; zero new provider requests; US$0.
The binding contract #205 is unchanged. Historical reports, labels, cassettes,
fixtures, ledgers, scorecards, SLOs and configuration defaults are unchanged.

The reviewed head was `0d709fd6`. Corrections are independently reviewable commits
on the existing draft #216, based on #215. #206–#215 refs remain unchanged;
#206's meaningful RED evidence is preserved. Do not merge the intermediate
stack independently of these corrections. This delivery deliberately does not
rebase the historical stack or overwrite its published evidence.

Tested implementation head: `be09303924995fd28ba5b69dda5405a5ab70426e`. The delivery head and its
exact CI runs are recorded in the PR description after this report is committed.

## Results

- 12,747 local tests passed, 8 skipped; 119 audit references resolve to executed
  passing cases. This is execution evidence, not proof of every acceptance clause.
- Ruff lint/format, mypy (758 files), seven import contracts and deptry passed.
- Audit: **98 covered, 20 partial, one confirmed mismatch (NC-76)**. Revision 1 was
  94/24/1; NC-111, NC-116, NC-118 and NC-119 now have additional boundary evidence.
- Local browser/live/container/postgres/simulator tests were excluded. The native
  JavaScript lifecycle test uses Node VM and fake APIs, without any browser page.
- Full suite recorded seven warnings, including a fake Cartesia websocket cleanup
  warning. That case passes isolated with the warning promoted to an error. It
  remains a reported cleanup concern; no external speech request was made.

## Stream restart and subject lifecycle (#207/#211)

Two original regressions failed before the fix: sequence 1/media timestamp 1
from a restarted stream was rejected after sequence 500; a late old-stream packet
could replace the successor. New same-ID/new-generation cases exercise the same
reset, and old-generation samples/frames never become a new confirmation.

Native sampler generations increase at each start and owner reassignment.
Sequence and media monotonicity apply within an incarnation. Retired native
incarnations stay retired across subjects; an old packet cannot be relabelled as
a newly assigned participant. Frame/sample cache keys include the generation,
and revalidation sees a new incarnation even when its sequence is lower. Its
content can match exactly while source continuity remains broken. No analyzed
capture or provider attempt is rewritten.

A separate pair of RED controls reproduced cross-subject relabelling and native
owner reassignment. The Node test runs the actual perception asset against fake
track processors: stable assignments preserve generation, owner/track changes
advance it, and late old-pump completion or an old track-ended callback cannot
stop/delete the successor. Native decoded-pixel hashing stability/CPU on a real
Meet page remains unmeasured and unauthorized.

## Writer acceptance and output (#209)

The unobserved-writer regression failed before the fix: the engine used its own
evaluation instant as `accepted_at`. It no longer does. The optional
`write_observed` capability returns writer-origin clock-tagged observations.
Pulse records acceptance at executor-thread entry and successful write completion
after the device call returns. Missing or foreign-clock observations remain
unproven; they are not relabelled onto the gate clock.

The write deadline is the earlier of evaluation + 250 ms and expiration of the
actual analyzed/confirmation sample's conservative freshness. Pulse opens the
device before checking that deadline, so device-open latency cannot bypass it.
A deterministic device fake also proves that a write entering at 100.1 and
returning at 100.4 records both real instants rather than inventing a timely write.

**V6 is still NO-GO.** The blocking Pulse API cannot promise that a call will
complete before its deadline, or that cancellation will revoke work already
running on the executor. A fake scheduling probe does not prove atomic final
validation plus physical acceptance. Completion-position sample bookkeeping is
observed by the event loop after return, not an independent physical-time
observer. These limitations are retained explicitly, not treated as compliance.

## Synthesis boundary and authorization (#213/#214)

Strict opt-in preparation is deferred until the engine checks a publication
binding. The actor, audio session and stream fleet forward visual classification.
Raw direct speech, a disguised no-visual call in a visual turn, unbound speculative
preparation and unapproved cue preparation are refused before entering a
synthesizer. Previously prepared model/cue audio is not written after visual
classification. No neutral cue catalog is invented.

Every retry rechecks the permission and local-only capability. Actual local
engine attempts have unique IDs, publication linkage and terminal outcomes;
preparation events are now named `runtime_preparation_*` and never represented
as physical provider requests. Local retry records explicitly say
`physical_provider_request=false`. A retry with permission revoked does not open
another synthesis attempt.

**A mode string is no longer enough to authorize a provider.** Provider mode
fails closed with `provider_attempt_ledger_unavailable`. This closes the unsafe
permission path; it does not implement or certify Y2. A ledger at every physical
HTTP/WebSocket/SDK retry boundary, explicit budget/grant accounting, cancellation
and unpublished outcomes are still required before any provider mode can be
made available. No such requests are authorized or executed here.

## NC-76 conflict: reproduced, not silently relaxed

The deterministic diagnostic `evals._publication_conflict.probe` produces this
order for one publication: local current synthesis requested/finished;
preliminary gate valid; final commit invalid (`scene_changed`); current PCM
prevented. The JSON trace is in `2026-11-15-publication-commit-conflict-r2.json`.
The current receipt truthfully contains no local audio. The mismatch remains.

For a dynamic current statement with no previously available authorized audio:

1. NC-76 requires a final committed-valid decision before synthesis starts.
2. Synthesis is required to obtain the first PCM frame.
3. I7 defines final commit as evaluation plus handoff of that first PCM frame,
   with no suspension between them.

That creates a causal cycle: commit → synthesis → first PCM → commit. A
preliminary approval is not that final commit. Moving synthesis after an earlier
“commit” introduces a wait; a 300 ms synthesis also violates V1's 250 ms bound.
Re-evaluating afterward makes it the final commit again, with synthesis preceding
it. This report does not rename either boundary to make the control pass.

No contract amendment was made. A future reviewed resolution could permit
strictly authorized local speculative rendering while retaining the final PCM
publication gate, or restrict dynamic current speech to already available legal
audio. Neither option is adopted here. The latter does not restore arbitrary
question-answer liveness. NC-76 remains a merge/acceptance blocker under #205.

## Product liveness (#215)

The evaluation was rerun at this implementation using identical read-only
cassettes and labels. Original result files are byte-identical. New artifacts:
`2026-11-15-support-matcher-offline-r2.json` and its Markdown report.

| Cassette / language | False support | False rejection | Correct-claim coverage | Abstention |
| --- | --- | --- | --- | --- |
| Answer-only EN | 0/22 | 12/19 (63.16%) | 7/19 (36.84%) | 34/41 (82.93%) |
| ID | No samples | Not measured | Not measured | Not measured |

**Product liveness remains NO-GO.** The safety corrections do not change the
textual evidence available to the matcher. Adding entity/attribute relations
from the question, weakening L1, changing labels, or promoting diagnostic
observed-facts results into runtime support would change the evidence contract;
none was done. Synthetic Indonesian results remain separate and do not fill the
cassette coverage gap.

## Final readiness matrix

| Workstream | Result | Remaining gate |
| --- | --- | --- |
| Restart lifecycle | RED → GREEN, native-generation/retired-packet tests | No real-page hashing or delivery proof |
| Writer observations | Engine timestamps removed; writer-source observations | Blocking device/cancellation; W1/W2/V6 unresolved |
| Model text synthesis | Deferred guarded reply/direct/cue/speculative/retry paths | Local fake proof only; external mode disabled |
| Per-attempt accounting | Preparation separated; local retry IDs/outcomes | Physical-provider grant/budget ledger unavailable |
| NC-76 | Reproduced mismatch with trace | Contract's dynamic-audio causal cycle requires review |
| Matcher product liveness | Evaluation unchanged, EN 36.84%, no ID cassettes | Product acceptance blocked |
| Participant audibility | No observation performed or inferred | Consent, clocks and observer gates unchanged |
| Production / Phase 5B | OFF / NO-GO | No activation or merge authorization |

## Corrective commits

- `3e9eb0f19e2dc81331befcb5216d7a34000d4180 fix(perception): distinguish restarted sampler generations`
- `19ad3ff560f955eeaa58a6d072ec7eb1671869c5 fix(audio): receipt writer observations instead of engine acceptance`
- `df421c86dd33f39acda819cda062d12d7f12d8e3 fix(speech): guard visual synthesis at every engine entry`
- `cb3bd971cbf9ca4f6e3db43b228111441af99d89 fix(perception): retire sampler identities across subject reassignment`
- `be09303924995fd28ba5b69dda5405a5ab70426e test(speech): distinguish preparation records from synthesis attempts`

## Full NC evidence index

The revision 1 requirements and all 119 IDs are retained in the JSON companion.
Covered means local deterministic assertions, partial means the row's remaining
clauses are not established, and blocked means an observed mismatch. No count
below is a production or participant-audibility verdict.

| Control | Status | Current evidence and remaining limits |
| --- | --- | --- |
| NC-01 | covered | `tests/test_publication_t6_regression.py::test_claim_about_a_figure_is_not_published_after_one_digit_changed`  |
| NC-02 | covered | `tests/test_publication_t6_regression.py::test_claim_about_a_figure_is_not_published_after_one_digit_changed`  |
| NC-03 | covered | `tests/test_publication_negative_controls.py::test_small_content_or_overlay_change_never_confirms_the_checked_figure` Synthetic one-digit table value and overlay on the claimed text. Real-page/native pixel sampling remains outside the authorized tests. |
| NC-04 | covered | `tests/test_publication_negative_controls.py::test_small_content_or_overlay_change_never_confirms_the_checked_figure` Synthetic one-digit table value and overlay on the claimed text. Real-page/native pixel sampling remains outside the authorized tests. |
| NC-05 | covered | `tests/test_publication_freshness.py::test_capture_age_near_the_limit_is_never_valid_inside_its_uncertainty`  |
| NC-06 | covered | `tests/test_publication_freshness.py::test_confirmation_age_near_the_limit_is_never_valid_inside_its_uncertainty`  |
| NC-07 | covered | `tests/test_publication_freshness.py::test_screen_content_decision_table`  |
| NC-08 | covered | `tests/test_publication_freshness.py::test_screen_content_decision_table`  |
| NC-09 | covered | `tests/test_publication_freshness.py::test_screen_content_decision_table`  |
| NC-10 | covered | `tests/test_publication_freshness.py::test_screen_content_decision_table`  |
| NC-11 | covered | `tests/test_publication_freshness.py::test_static_slide_whose_last_exact_sample_is_stale_is_prevented`  |
| NC-12 | covered | `tests/test_commit_boundary.py::test_confirmation_fresh_at_the_preliminary_decision_and_old_at_the_commit`  |
| NC-13 | covered | `tests/test_publication_activation.py::test_static_slide_without_a_confirmation_sample_is_prevented`  |
| NC-14 | covered | `tests/test_revalidation_match.py::test_camera_heartbeat_without_a_record_is_unknown`  |
| NC-15 | covered | `tests/test_samples_ledger.py::test_repeated_media_timestamp_is_not_a_new_capture`  |
| NC-16 | covered | `tests/test_samples_ledger.py::test_lower_or_equal_sequence_is_discarded`  |
| NC-17 | covered | `tests/test_samples_ledger.py::test_higher_sequence_with_an_earlier_capture_time_is_marked_inconsistent`  |
| NC-18 | covered | `tests/test_samples_ledger.py::test_skipped_hash_is_stored_without_reusing_the_previous_digest`  |
| NC-19 | covered | `tests/test_content_digest.py::test_records_that_are_not_comparable_say_nothing_about_content`  |
| NC-20 | covered | `tests/test_content_digest.py::test_records_that_are_not_comparable_say_nothing_about_content`  |
| NC-21 | covered | `tests/test_publication_identity.py::test_another_slide_is_a_changed_scene_end_to_end`  |
| NC-22 | covered | `tests/test_publication_identity.py::test_restarted_share_with_the_identical_slide_does_not_prove_the_source`<br>`tests/test_samples_ledger.py::test_restarted_stream_accepts_reset_sequence_and_media_timestamp`  |
| NC-23 | partial | `tests/test_publication_identity.py::test_same_content_shown_by_another_presenter_is_a_changed_source` Presenter-change source attribution is tested; the state gate is separate. No one combined pixel-identical-presenter-transition test covers all clauses. |
| NC-24 | covered | `tests/test_publication_identity.py::test_content_that_returned_after_another_slide_is_a_changed_source`  |
| NC-25 | covered | `tests/test_publication_identity.py::test_screen_identity_table`<br>`tests/test_samples_ledger.py::test_late_retired_stream_cannot_replace_the_restarted_stream`  |
| NC-26 | covered | `tests/test_publication_identity.py::test_replaced_camera_stream_of_the_same_participant_is_not_the_same_scene`  |
| NC-27 | covered | `tests/test_publication_identity.py::test_camera_stream_that_now_belongs_to_another_participant_is_a_changed_subject`<br>`tests/test_samples_ledger.py::test_retired_camera_sampler_cannot_be_relabelled_as_a_new_subject`  |
| NC-28 | partial | `tests/test_publication_identity.py::test_camera_identity_table` Similar/distinct camera outcomes are deterministic table inputs; the exact pose/second-person image sequence is not a dedicated fixture. |
| NC-29 | partial | `tests/test_publication_identity.py::test_camera_identity_table` Similar/distinct camera outcomes are deterministic table inputs; the exact pose/second-person image sequence is not a dedicated fixture. |
| NC-30 | partial | `tests/test_publication_identity.py::test_camera_identity_table` Similar/distinct camera outcomes are deterministic table inputs; the exact pose/second-person image sequence is not a dedicated fixture. |
| NC-31 | partial | `tests/test_publication_identity.py::test_camera_identity_table` Exact camera and historical-only renderer are tested separately; no combined frozen-camera advancing-media-timestamps end-to-end case. |
| NC-32 | partial | `tests/test_revalidation_match.py::test_repeated_frame_does_not_become_the_confirmation` Repeated-frame filtering is tested; frozen camera past 5 s is not a dedicated combined case. |
| NC-33 | covered | `tests/test_publication_identity.py::test_camera_identity_table`  |
| NC-34 | covered | `tests/test_publication_freshness.py::test_screen_content_decision_table`  |
| NC-35 | covered | `tests/test_claims_support.py::test_claim_without_a_look_of_its_kind_is_missing_evidence`  |
| NC-36 | covered | `tests/test_abstention_catalog.py::test_look_that_inspected_no_frame_is_answered_with_the_uninspected_entry`  |
| NC-37 | covered | `tests/test_publication_negative_controls.py::test_required_snapshot_missing_never_becomes_valid`  |
| NC-38 | covered | `tests/test_commit_boundary.py::test_non_exact_sample_before_the_commit_hands_nothing_to_the_writer`  |
| NC-39 | covered | `tests/test_commit_boundary.py::test_non_exact_sample_before_the_commit_hands_nothing_to_the_writer`  |
| NC-40 | covered | `tests/test_commit_boundary.py::test_non_exact_sample_before_the_commit_hands_nothing_to_the_writer`  |
| NC-41 | covered | `tests/test_commit_boundary.py::test_exact_sample_before_the_commit_is_recorded_on_the_committed_decision`  |
| NC-42 | covered | `tests/test_commit_boundary.py::test_non_exact_sample_before_the_commit_hands_nothing_to_the_writer`  |
| NC-43 | covered | `tests/test_commit_boundary.py::test_sample_after_the_commit_is_evidence_and_leaves_the_decision_alone`  |
| NC-44 | covered | `tests/test_commit_boundary.py::test_sample_after_the_commit_is_evidence_and_leaves_the_decision_alone`  |
| NC-45 | covered | `tests/test_commit_boundary.py::test_freeze_after_the_preliminary_decision_commits_after_the_thaw`  |
| NC-46 | partial | `tests/test_commit_boundary.py::test_no_suspension_point_lies_between_the_final_evaluation_and_the_handoff` Engine timing is no longer substituted for writer acceptance. Pulse measures executor entry and write return. Atomic validation-to-physical-acceptance and preventing an in-progress blocking write remain unproved (V6). |
| NC-47 | covered | `tests/test_commit_boundary.py::test_change_after_the_first_write_does_not_rewrite_the_decision`  |
| NC-48 | covered | `tests/test_commit_boundary.py::test_committed_attempt_cancelled_before_its_first_write_is_not_spoken`  |
| NC-49 | covered | `tests/test_commit_boundary.py::test_committed_attempt_whose_first_write_fails_is_not_spoken`  |
| NC-50 | partial | `tests/test_publication_negative_controls.py::test_zero_byte_clip_is_not_counted_or_recorded_as_speech` Zero-byte output is unspoken; no first frame means the current implementation never reaches a commit, so the exact committed-valid/zero-byte combination is not established. |
| NC-51 | partial | `tests/test_commit_boundary.py::test_withheld_write_whose_scene_changed_during_the_delay_is_prevented` Delay and changed scene tested, but readiness/back-pressure preceding final validation is not demonstrated for production W2. |
| NC-52 | covered | `tests/test_commit_boundary.py::test_committed_attempt_cancelled_before_its_first_write_is_not_spoken`  |
| NC-53 | covered | `tests/test_commit_boundary.py::test_sample_after_the_commit_is_evidence_and_leaves_the_decision_alone`  |
| NC-54 | covered | `tests/test_publication_negative_controls.py::test_each_catalog_entry_has_exactly_one_audio_receipt_in_each_language`  |
| NC-55 | covered | `tests/test_abstention_catalog.py::test_look_that_inspected_no_frame_is_answered_with_the_uninspected_entry`  |
| NC-56 | covered | `tests/test_abstention_catalog.py::test_text_that_is_not_exactly_an_entry_is_refused`  |
| NC-57 | covered | `tests/test_abstention_catalog.py::test_insufficient_look_is_answered_with_the_inspected_frame_entry`  |
| NC-58 | covered | `tests/test_abstention_catalog.py::test_insufficient_look_is_answered_with_the_inspected_frame_entry`  |
| NC-59 | covered | `tests/test_abstention_catalog.py::test_text_that_is_not_exactly_an_entry_is_refused`  |
| NC-60 | covered | `tests/test_visual_speech.py::test_model_declared_scope_or_extra_field_voids_the_proposal`  |
| NC-61 | covered | `tests/test_visual_speech_fallback.py::test_two_prevented_statements_that_cannot_fall_back_get_one_abstention`  |
| NC-62 | covered | `tests/test_abstention_catalog.py::test_reference_to_a_version_language_or_entry_that_does_not_exist_is_refused`  |
| NC-63 | covered | `tests/test_visual_speech.py::test_extra_assertion_in_the_model_sentence_never_reaches_the_synthesizer`  |
| NC-64 | covered | `tests/test_visual_speech.py::test_extra_assertion_in_the_model_sentence_never_reaches_the_synthesizer`  |
| NC-65 | covered | `tests/test_visual_speech.py::test_extra_assertion_in_the_model_sentence_never_reaches_the_synthesizer`  |
| NC-66 | partial | `tests/test_visual_speech.py::test_claim_that_is_not_supported_or_not_renderable_is_never_spoken` Polarity matching and raw extra-negation exclusion are tested; the exact Costs approved/negative proposal case is not separately asserted. |
| NC-67 | covered | `tests/test_visual_speech.py::test_extra_assertion_in_the_model_sentence_never_reaches_the_synthesizer`  |
| NC-68 | covered | `tests/test_rendering_patterns.py::test_value_field_that_carries_more_than_a_value_is_not_rendered`  |
| NC-69 | covered | `tests/test_rendering_patterns.py::test_every_listed_marker_blocks_a_field_in_its_language`  |
| NC-70 | covered | `tests/test_rendering_patterns.py::test_entity_field_with_a_marker_punctuation_or_excess_length_is_not_rendered`  |
| NC-71 | partial | `tests/test_rendering_patterns.py::test_missing_field_span_is_not_rendered` Missing span tested. Forged/cross-clause span provenance is not independently validated by render; it trusts Support supplied by the matcher. |
| NC-72 | covered | `tests/test_rendering_patterns.py::test_value_field_that_carries_more_than_a_value_is_not_rendered`  |
| NC-73 | partial | `tests/test_rendering_patterns.py::test_negative_claim_has_no_pattern` Supported negative is rejected by renderer. Conditional proposals are rejected at parsing, with no dedicated conditional rendering reason assertion. |
| NC-74 | partial | `tests/test_claims_support.py::test_malformed_or_extended_proposal_yields_no_claims` Unsupported shape fails proposal parsing; required claim_render_unsupported reason and AB1 path are not a dedicated case. |
| NC-75 | covered | `tests/test_visual_speech.py::test_turn_language_without_a_pattern_abstains`  |
| NC-76 | blocked | `tests/test_visual_speech_fallback.py::test_preparation_for_prevented_publication_is_still_journaled` Confirmed by evals._publication_conflict.probe: a valid preliminary decision can be followed by an invalid final commit after local current synthesis. I7 requires PCM at commit; NC-76 requires commit before synthesis. No contract change or readiness claim. |
| NC-77 | covered | `tests/test_rendering_patterns.py::test_supported_labelled_case_renders_only_from_spans_of_the_answer`  |
| NC-78 | covered | `tests/test_visual_speech.py::test_same_claim_declared_twice_is_spoken_once`  |
| NC-79 | covered | `tests/test_visual_speech.py::test_visual_turn_without_declared_claims_speaks_one_abstention_only`  |
| NC-80 | covered | `tests/test_visual_speech.py::test_visual_turn_without_declared_claims_speaks_one_abstention_only`  |
| NC-81 | covered | `tests/test_visual_speech_fallback.py::test_statement_prevented_by_age_falls_back_by_the_age_of_the_checked_frame`  |
| NC-82 | covered | `tests/test_visual_speech.py::test_extra_assertion_in_the_model_sentence_never_reaches_the_synthesizer`  |
| NC-83 | covered | `tests/test_visual_speech_fallback.py::test_model_written_historical_binding_is_refused_under_a_render_only_context`  |
| NC-84 | covered | `tests/test_visual_speech_fallback.py::test_historical_statement_is_limited_to_thirty_seconds_from_the_analyzed_capture`  |
| NC-85 | covered | `tests/test_visual_speech_fallback.py::test_thirty_seconds_are_measured_from_the_checked_frame_not_the_confirmation`  |
| NC-86 | covered | `tests/test_visual_speech_fallback.py::test_camera_stream_that_changed_subject_gets_the_abstention`  |
| NC-87 | covered | `tests/test_visual_speech_fallback.py::test_one_claim_falls_back_and_one_unsupported_claim_gets_the_abstention`  |
| NC-88 | partial | `tests/test_visual_speech_fallback.py::test_historical_statement_links_the_prevented_publication_and_shares_its_evidence` ID, evidence and replacement linkage covered; no single test asserts exactly unchanged vision/LLM call counts after gather. |
| NC-89 | covered | `tests/test_visual_speech_fallback.py::test_historical_gather_is_rendered_historically_without_a_freshness_check`  |
| NC-90 | covered | `tests/test_visual_speech_fallback.py::test_historical_gather_with_an_unsupported_claim_abstains`  |
| NC-91 | covered | `tests/test_visual_speech_fallback.py::test_historical_gather_without_a_mapped_capture_abstains`  |
| NC-92 | partial | `tests/test_visual_speech.py::test_two_claims_are_two_statements_decided_separately`<br>`tests/test_publication_negative_controls.py::test_screen_and_camera_have_independent_publications` Mixed-source publications have separate bindings; the exact screen-valid/camera-unknown combined decision remains only partly exercised. |
| NC-93 | covered | `tests/test_claims_support.py::test_labelled_support_case`  |
| NC-94 | covered | `tests/test_claims_support.py::test_labelled_support_case`  |
| NC-95 | covered | `tests/test_claims_support.py::test_labelled_support_case`  |
| NC-96 | covered | `tests/test_claims_support.py::test_labelled_support_case`  |
| NC-97 | covered | `tests/test_visual_speech.py::test_visual_turn_without_declared_claims_speaks_one_abstention_only`  |
| NC-98 | covered | `tests/test_claims_support.py::test_claim_declared_against_evidence_of_another_source_is_a_mismatch`  |
| NC-99 | covered | `tests/test_claims_support.py::test_labelled_support_case`  |
| NC-100 | covered | `tests/test_visual_speech.py::test_model_declared_scope_or_extra_field_voids_the_proposal`  |
| NC-101 | covered | `tests/test_publication_negative_controls.py::test_second_independent_statement_can_publish_after_first_is_prevented`  |
| NC-102 | covered | `tests/test_publication_activation.py::test_fresh_supported_screen_claim_is_published_and_counted` Entire activation module is run, not merely this representative case; historical behavior changed as specified. |
| NC-103 | covered | `tests/test_publication_activation.py::test_no_production_module_composes_the_opt_in_gate`  |
| NC-104 | partial | `tests/test_visual_speech_fallback.py::test_real_speech_provider_is_never_constructed_in_a_fallback_turn` Local fakes and provider-registry refusal cover fallback. Universal vision/LLM call-count assertions across every gate/revalidator/renderer path are not exhaustive. |
| NC-105 | covered | `tests/test_revalidation_match.py::test_analyzed_record_is_not_changed_by_later_samples`<br>`tests/test_sensors_samples.py::test_sampler_restart_resets_counters_without_binding_a_retired_frame`  |
| NC-106 | covered | `tests/test_visual_speech.py::test_turn_without_visual_needs_speaks_the_model_text_as_today`  |
| NC-107 | covered | `tests/test_commit_boundary.py::test_first_write_delayed_past_the_validation_window_is_withheld`  |
| NC-108 | partial | `tests/test_commit_boundary.py::test_withheld_write_is_re_evaluated_and_published_when_still_valid` Deterministic withheld-write re-evaluation succeeds. Production blocking-write atomicity and cancellation are still activation blockers. |
| NC-109 | covered | `tests/test_commit_boundary.py::test_withheld_write_whose_scene_changed_during_the_delay_is_prevented`  |
| NC-110 | covered | `tests/test_commit_boundary.py::test_late_write_that_could_not_be_prevented_is_reported_and_marked_unproven`  |
| NC-111 | covered | `tests/test_commit_boundary.py::test_missing_or_out_of_order_writer_acceptance_cannot_prove_freshness`<br>`tests/test_commit_boundary.py::test_writer_instants_from_another_clock_are_not_relabelled_as_local`<br>`tests/test_commit_boundary.py::test_engine_evaluation_time_cannot_substitute_for_unobserved_writer_acceptance`<br>`tests/test_pulse_writer.py::test_observed_write_records_writer_thread_acceptance_and_completion` Writer-origin timing is receipted; missing or foreign-clock observations are unproven. These tests do not close the blocking-device V6 gate. |
| NC-112 | covered | `tests/test_commit_boundary.py::test_committed_attempt_cancelled_before_its_first_write_is_not_spoken`  |
| NC-113 | covered | `tests/test_visual_speech_fallback.py::test_without_authorized_synthesis_a_visual_turn_is_silent_and_journaled`  |
| NC-114 | partial | `tests/test_visual_speech_fallback.py::test_real_speech_provider_is_never_constructed_in_a_fallback_turn` Targeted fallback registry refusal and fake synthesizer are proven. Whole legacy suite may construct mocked real client classes; no universal constructor prohibition exists. |
| NC-115 | partial | `tests/test_playback_synthesis_boundary.py::test_each_actual_local_retry_has_its_own_attempt_and_terminal_record`<br>`tests/test_playback_synthesis_boundary.py::test_retry_rechecks_local_authorization_before_opening_another_attempt`<br>`tests/test_visual_speech_fallback.py::test_provider_mode_without_a_physical_attempt_ledger_fails_closed`<br>`tests/test_visual_speech_fallback.py::test_failed_and_cancelled_preparation_requests_are_journaled` Preparation is explicitly separate from actual local engine attempts. Local retries have unique IDs/outcomes and recheck permission. Provider mode is disabled: no physical-provider-attempt authorization/budget ledger is claimed or implemented. |
| NC-116 | covered | `tests/test_playback_synthesis_boundary.py::test_render_only_speculative_preparation_cannot_start_unbound_synthesis`<br>`tests/test_playback_synthesis_boundary.py::test_render_only_cue_preparation_cannot_synthesize_an_unapproved_sentence`<br>`tests/test_visual_speech_fallback.py::test_nothing_is_synthesized_ahead_of_a_selected_statement` Strict opt-in preparation is deferred until binding checks; unapproved cue preparation is refused. Authorized runtime local statements retain their own preparation path. |
| NC-117 | partial | `tests/test_visual_speech_fallback.py::test_without_authorized_synthesis_a_visual_turn_is_silent_and_journaled` Unavailable synthesis silence is tested. No existing-audio-only mode/catalog cache exists to execute the exact dynamic-historical-with-catalog-only case. |
| NC-118 | covered | `tests/test_playback_synthesis_boundary.py::test_direct_speech_cannot_bypass_a_registered_visual_turn`<br>`tests/test_playback_synthesis_boundary.py::test_render_only_cue_preparation_cannot_synthesize_an_unapproved_sentence`<br>`tests/test_playback_synthesis_boundary.py::test_preexisting_cue_audio_cannot_bypass_the_render_only_boundary`<br>`tests/test_playback_synthesis_boundary.py::test_render_only_speculative_preparation_cannot_start_unbound_synthesis`<br>`tests/test_playback_synthesis_boundary.py::test_retry_rechecks_local_authorization_before_opening_another_attempt`<br>`tests/test_visual_speech.py::test_extra_assertion_in_the_model_sentence_never_reaches_the_synthesizer`<br>`tests/test_visual_speech.py::test_streamed_reply_is_withheld_whole_and_yields_nothing_to_speak` Local deterministic entry-boundary controls cover reply, direct, cue, speculative/lookahead preparation and retry. Engine checks and visual classification are forwarded through actor/audio/fleet. External provider paths remain disabled, not certified. |
| NC-119 | covered | `tests/test_playback_synthesis_boundary.py::test_previously_prepared_model_audio_is_discarded_after_visual_classification`<br>`tests/test_playback_synthesis_boundary.py::test_render_only_speculative_preparation_cannot_start_unbound_synthesis` Audio prepared before visual classification is discarded with no write; strict unbound preparation does not start synthesis. |

JUnit SHA-256: `63a97cc2ac4adbf66aae19f229ee0c9bf8312c8a6362d51d26f6153a36c18eba`.
Reproduce the offline suite with `.venv/bin/pytest -n 4 -q -m 'not browser and not live and not container and not postgres and not simulator' --junitxml=/private/tmp/fakeyou-review-revision.xml`.
