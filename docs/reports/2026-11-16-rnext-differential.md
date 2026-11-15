# rNext: bounded early-commit differential investigation

Recommendation: **MERGE HOLD remains. Attribution INCONCLUSIVE.** Four green
executions did not reproduce or explain the recorded failure. No historical
failure is waived, and no merge, mark-ready or production activation occurred.
PublicationContext OFF; Phase 5B NO-GO; V6 NO-GO; zero new provider requests/spend.

## Exact inputs and bounded method

- Unchanged main: `a79197b5cf20d4235861b46edeb20165f4112bed`.
- PR #220: `77e51c797ddbc9df75a5eeeac73331d306cdd2e1`.
- Frozen audit record PR #221: `44a5739ea74ff2ebdd38ae54b768e36dc5fd593a`.
- Exactly four full native offline suites: main, PR, PR, main. Four xdist workers
  each, serial runs, no parallel RustFS testing, no added artificial stress,
  timeout 600 s per run. No extra run was made to obtain green.
- Same project Python 3.12.13 interpreter and dependencies on macOS 27.0.1 arm64;
  10 logical CPUs and 16 GiB memory. PYTHONHASHSEED=0, exact worktree imports,
  provider/test-DB settings removed and non-local outbound sockets denied.
- Existing default marker exclusions retained. No source/test/assertion/timing/
  skip-marker change, scheduler replacement, clock mocking or safety change.
  Both worktrees were clean before and after every run.

An external pytest plugin installs passive `sys.settrace` only during the target
case, records `time.monotonic_ns()`, then restores the previous trace. It observes
actor/task calls, asyncio scheduling, the fake responder/audio and test lines;
it does not wrap or change their return values. No previous trace was present.
Its overhead can perturb scheduling; this is not an uninstrumented control or a
reconstruction of the old failed execution. Exact plugin/protocol/runner hashes,
commands and trace timestamps are in the JSON companion and local artifacts.

## Measured executions: none omitted

Counts are passed / failed / skipped. Duration includes the pytest process and
its teardown, not Docker snapshots. All four exit codes were zero.

| Run | Revision | Counts | Duration, s | Warnings |
| --- | --- | --- | --- | --- |
| 01-main | `a79197b5` | 12,053 / 0 / 113 | 216.130 | 6 |
| 02-pr220 | `77e51c79` | 12,153 / 0 / 113 | 156.594 | 6 |
| 03-pr220 | `77e51c79` | 12,153 / 0 / 113 | 156.397 | 6 |
| 04-main | `a79197b5` | 12,053 / 0 / 113 | 152.814 | 7 |

The target ran on `gw0` in all four executions, following the same twelve tests.
Within each revision, repeated collection hashes and executed node sets matched.
The native suites are comparable full workloads, not identical collections:
main has 12,166 executed nodes including skips; PR has 12,266. There are 12,109
identical raw node IDs, 57 main-only IDs and 157 PR-only IDs. Main-only IDs are
source-location-derived taxonomy parameters; PR-only IDs comprise 57 ported
functional cases, 40 source-rule cases and 60 taxonomy parameters. The full ID
lists are retained in JSON. No special deselection made the workloads match.

Host load was not constant: target-start 1/5/15-minute averages were
20.877/12.965/10.125, 19.865/16.567/12.208, 15.140/16.476/12.904, and
33.439/25.963/17.576. Existing Docker workers were already running/restarting;
they were observed, not stopped or reconfigured. Before/after Docker state,
load and process snapshots were recorded. Exact worktree paths differ by design;
cache warmth and subsecond scheduling were not controlled. These differences
limit attribution and are not silently normalized away.

The fourth run retains the fake Cartesia websocket cleanup
`PytestUnraisableExceptionWarning`; the six other warnings per run are Pydantic
deprecations. No live Cartesia request was made. A passing test with that warning
is not a claim the cleanup issue was fixed.

## Measured monotonic causal sequence

Values below are milliseconds after that run's `target_call_start_ns`, retained
as absolute monotonic nanoseconds in JSON. The nominal due column is sleep-entry
plus the unchanged 200 ms hold; scheduling calls/handles are also in the trace.
Fake audio completion is an in-process fake `speak` return, not physical output.

| Run | Hold sleep entered | Nominal due | Hold resumed | Fake audio complete | Assertion | Audio-to-assert margin |
| --- | --- | --- | --- | --- | --- | --- |
| 01-main | 19.025 | 219.025 | 220.789 | 251.161 | 284.333 | 33.172 |
| 02-pr220 | 19.117 | 219.118 | 220.745 | 244.255 | 301.569 | 57.314 |
| 03-pr220 | 27.116 | 227.116 | 229.403 | 247.913 | 294.097 | 46.184 |
| 04-main | 21.219 | 221.219 | 223.531 | 239.484 | 292.515 | 53.031 |

In every trace the first interim changed the text, installed timer version 1,
and the repeated interim returned `changed=false` with the same version and
timer identity. The assertion observed the expected synthetic sentence after
fake speech completed. These are observations of successful runs only.

## Historical failures retained

- The preceding uninstrumented #220 full run remains **12,152 passed,
  113 skipped, one failed**, at
  `tests/test_actor_early_commit.py::test_a_repeated_interim_does_not_restart_the_hold`.
  It observed `audio.spoken == []` at line 377. Its JUnit SHA-256 remains
  `257a8cf7c7a558e60cff3970980d6376a00f8bf18ab96808e0ac7dadcbfd4569`.
- The earlier reported worktree run at
  `36da8ca296b59920e01b4189a647ec6376eff56b` retains its two failures:
  `test_resource_handle_is_the_bundled_file_on_disk` and
  `test_whole_respond_over_the_turn_budget_is_cut_journaled_and_counted`.
  Its provenance remains the historical PR body; the original raw log was not
  recovered. New passes do not rewrite or waive those failures.

No new failure occurred in this bounded experiment. That absence neither
invalidates the prior failure nor proves it harmless. The prior exact-head CI
success is kept separate and was not rerun as a substitute for this experiment.

## Source arguments, hypotheses and unproven items

**Source arguments:** actor bytes are identical at both revisions, SHA-256
`44e8fec9ec9ed67685388a3dc574339ddff2ba4f96e6bf777803b1adc8dc272d`.
Target test bytes are identical, SHA-256
`ec5b2ccb2d62de1c29c37ae3123d40528c7f210974aec1ac2fd7d67fb9c1ba5f`.
`note_interim` only rearms when the normalized text changes. The hold is an async
task whose 200 ms sleep starts when that coroutine executes. The test waits
0.12 s and 0.14 s and then asserts downstream fake audio without waiting for its
completion event. These properties permit scheduling sensitivity on both trees.
They do not establish the historical cause or rule out transitive branch effects.

**Hypotheses:** scheduling delay or downstream turn work could consume the
nominal 60 ms headroom; trace overhead, hash seed, cache state or external host
activity could affect recurrence. No hypothesis is promoted to a measured cause.
A high load average is not proof of event-loop starvation at a particular instant.

**Unproven:** the actual failing causal sequence, reproducibility on unchanged
main under the failed execution's exact environment, absence of any PR-specific
transitive influence, and the product impact of the failure. The old execution
has no causal trace; it cannot be reconstructed from later successes.

## Recommendation and authorization state

Keep **MERGE HOLD**. The investigation supplies measured successful causal
sequences and narrows the evidence; it does not supply a failure explanation,
a fix or a waiver. Further investigation or explicit failure disposition is a
human decision, not an automatic consequence of four green runs. The four-run
budget is exhausted; no extra stress sweep or implementation fix was started.

PR #220 remains open/draft at the reviewed SHA; remote main remains `a79197b5`.
PR B's scope is accepted and conditionally authorized by the ruling, but its
precondition is not met: #220 has not merged. The ruling supplies implementation
permission once that condition is verified. No PR B implementation branch was
created. When it actually merges, verify the new main SHA and use a fresh branch
for only the accepted 17-control plan; draft PR B still needs human merge review.

PR #221 remains a frozen draft audit record at `44a5739e`. This revision changes
no original report or stacked ancestry. No merge of that ancestry is proposed.
If reports are later required on main, a separate docs-only extraction with its
precise diff must be presented. #205 and original legacy PR refs remain unchanged.

Local raw logs, JUnit, full traces, runner and probe remain under
`/private/tmp/fakeyou-rnext-20261116/`; hashes are committed in the JSON companion.
This is local artifact retention, not independent custody. **EVIDENCE CUSTODY
PENDING** remains in force.
