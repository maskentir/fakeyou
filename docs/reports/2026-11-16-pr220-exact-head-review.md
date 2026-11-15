# PR #220 exact-head review

Disposition: **MERGE HOLD.** Requested verification is recorded below; the new
full local run failed. No test was changed, skipped or marked xfail to hide a
failure. No approval, mark-ready or merge was issued. PublicationContext OFF;
Phase 5B NO-GO; V6 NO-GO; zero new provider requests/spending; no activation.

## Exact revisions and equivalence

| Item | Observed value |
| --- | --- |
| Reviewed PR #220 head | `77e51c797ddbc9df75a5eeeac73331d306cdd2e1` |
| Earlier worktree test commit | `36da8ca296b59920e01b4189a647ec6376eff56b` |
| Whole tree at both commits | `d91907ebfcc4453740ead1b600d5b1e2b26c9446` |
| Parent of both commits / current remote main | `a79197b5cf20d4235861b46edeb20165f4112bed` |
| Whole-tree diff between test and PR commits | Empty |
| Port scope | 14 paths: eight production files and six test files |
| Port provenance | All 14 paths byte-identical to #207 at `3f50b4f95d4927a6816fd7f55120ea8cd60af92a` |

The commits differ in commit metadata, not tree or parent. Whole-tree
equivalence transfers source content, not an independently verified test run:
the earlier results are still identified as historical results below.

Source review covered the digest/sample model, sampler asset, ledger, frame
binding, timeline, evidence context, revalidation and content decision, plus
their six ported test files. No audio writer, speech rendering, model parsing,
provider configuration or activation path is added by this diff. Known sampler
restart/page-identity limitations are explicitly outside #220's extraction and
are addressed in the PR B plan; they have not been silently declared fixed here.

## Newly observed execution

Tests used the project Python 3.12.13 interpreter with `PYTHONPATH` starting at
the relevant exact worktree's `src` and root. Imported module paths were
checked. This avoids accidentally testing the legacy source in the editable
checkout while collecting tests from #220. The head and baseline worktrees
remain clean, detached at the exact commits above.

| Execution | Source SHA | Observed result |
| --- | --- | --- |
| Synthetic browser, `tests/test_sensors_browser.py`, `-m browser` | `77e51c79` | **4 passed**, 1 warning |
| Full offline suite, `pytest -n 4 -q` with existing default marker exclusions | `77e51c79` | **12,152 passed, 113 skipped, 1 failed**, 6 warnings; exit 1 |
| Five ported non-browser test files plus three individually selected cases; browser file deselected by default | `77e51c79` | **60 passed, 4 deselected**, 1 warning; 57 ported cases plus three selected cases |
| Two historical failure nodes, selected in isolation | `a79197b5` | **2 passed**, 1 warning; does not replace their historical failures |
| New early-commit failure node, selected in isolation | `a79197b5` | **1 passed**, 1 warning; no baseline failure reproduced by this isolated run |

The head's focused run includes the same two historical nodes and the new
early-commit failure node. They pass there. No full-suite failure is converted
to a pass by those isolated results, and no full rerun was performed merely to
obtain a green summary.

The browser used cached Chromium `145.0.7632.109.2`, an explicit local binary
override, no license key and auto-update disabled. The process and children ran
under a macOS sandbox denying outbound networking except localhost. A separate
non-local connection control returned `PermissionError`, errno 1. No browser
binary was downloaded. The fixture uses `about:blank` and locally fulfilled
`https://perception.invalid`; no Meet page or provider experiment was run.

The four browser cases cover protocol-driven screen observation, peer states,
an unchanged pixel digest with advancing media timestamps, and a one-pixel
change under the thumbnail threshold. They do not measure codec stability,
real Meet performance, identity after a real reload, or production readiness.

## Failures retained, with provenance

The PR body reports this historical run at `36da8ca2`: **12,151 passed,
113 skipped, 91 deselected, two failed**, with both failures also reported on
unmodified `main` at `a79197b5`. Its original raw log was not recovered in this
review, so that statement is attributed to the PR body rather than described
as newly executed evidence.

| Historical failure | Disposition |
| --- | --- |
| `tests/test_resources.py::test_resource_handle_is_the_bundled_file_on_disk` | Retained as a historical failure; no fix or waiver. Newly observed isolated passes are recorded separately |
| `tests/test_turn_graph_nonstreamed.py::test_whole_respond_over_the_turn_budget_is_cut_journaled_and_counted` | Retained as a historical failure; no fix or waiver. Newly observed isolated passes are recorded separately |

Newly observed full-run failure:

`tests/test_actor_early_commit.py::test_a_repeated_interim_does_not_restart_the_hold`

At `tests/test_actor_early_commit.py:377`, `audio.spoken` was `[]` while the
assertion expected `['Rani, when is the beta?']`. The test uses a 0.2 s hold and
real sleeps of 0.12 s and 0.14 s. The test and actor source are unchanged from
`main`; this is evidence about scope, not proof that the failure is pre-existing
or harmless. It passed in the focused head run and isolated baseline run. Root
cause, repeatability under the full workload and attribution remain unproven.
The failed full run stays failed. No assertion, timing, expectation or marker
was edited, and this review does not waive the failure.

Warnings in these new runs include Pydantic deprecations. PostgreSQL-dependent
cases were skipped without an external database; optional `sherpa_onnx` and
missing recorded data also remain skipped. These are existing environment
exclusions, not newly applied failure masks.

## Exact-head CI

GitHub check-run records name
`77e51c797ddbc9df75a5eeeac73331d306cdd2e1` as their head SHA.

| Run | Observed result |
| --- | --- |
| [Foundation 43747722393](https://github.com/maskentir/fakeyou/actions/runs/43747722393) | Success: Python **12,258 passed, 8 skipped, 91 deselected**, 2 warnings; mypy 739 files clean; Ruff, seven import contracts, deptry, build, Compose and dependency audit succeeded; no known vulnerabilities found |
| [Meeting simulator 43747722420](https://github.com/maskentir/fakeyou/actions/runs/43747722420) | Replay success on the exact head |
| [Earlier Foundation 43746782346](https://github.com/maskentir/fakeyou/actions/runs/43746782346) | Python, Node and secrets check-run records also report success on the same head |

CI has a different environment from the local run. Its green result is not a
reason to discard local failure evidence, waive baseline failures, satisfy V6,
or activate publication. No new CI rerun or provider evaluation was requested.

## Reproduction and artifact integrity

Local text logs and JUnit files are retained under
`/private/tmp/fakeyou-next-directive-20261116/`. They are local execution
artifacts, not independent evidence custody. Exact hashes, failure messages,
source paths and check-run URLs are in the JSON companion.

| JUnit artifact | SHA-256 |
| --- | --- |
| `pr220-browser.xml` | `8f704dbd301598b7798647672d35b26c14d1db5223c3eb4cb3505a4772a0b33d` |
| `pr220-offline.xml` | `257a8cf7c7a558e60cff3970980d6376a00f8bf18ab96808e0ac7dadcbfd4569` |
| `pr220-focused.xml` | `eca1312f9ab7270238a95b3abaa2a6729816fcb0c5331feaa75c4f4f35a1d720` |

Commands used for the head, with the corresponding exact worktree as cwd:

```sh
git diff --exit-code 36da8ca2 77e51c79
git rev-parse '36da8ca2^{tree}' '77e51c79^{tree}'
```

The test prefix was `env -u TEST_DATABASE_URL -u FAKEYOU_TEST_POSTGRES`, with
`PYTHONPATH=/Users/pushmove/.codex/worktrees/97ea/fakeyou/src:/Users/pushmove/.codex/worktrees/97ea/fakeyou`,
followed by `sandbox-exec -f /private/tmp/fakeyou-next-directive-20261116/offline.sb`
and the project interpreter `/Users/pushmove/Desktop/side_projects/jemboed/fakeyou/.venv/bin/python`.
Browser execution additionally removed `CLOAKBROWSER_LICENSE_KEY` and set
`CLOAKBROWSER_AUTO_UPDATE=false` and `CLOAKBROWSER_BINARY_PATH` to the cached
binary recorded in the JSON companion. No repository test or source patch was
used to isolate the network.

```sh
python -m pytest -q -m browser tests/test_sensors_browser.py --junitxml=/private/tmp/fakeyou-next-directive-20261116/pr220-browser.xml
python -m pytest -n 4 -q --junitxml=/private/tmp/fakeyou-next-directive-20261116/pr220-offline.xml
python -m pytest -q tests/test_content_digest.py tests/test_samples_ledger.py tests/test_revalidation_match.py tests/test_sensors_samples.py tests/test_publication_t6_regression.py tests/test_sensors_browser.py tests/test_resources.py::test_resource_handle_is_the_bundled_file_on_disk tests/test_turn_graph_nonstreamed.py::test_whole_respond_over_the_turn_budget_is_cut_journaled_and_counted tests/test_actor_early_commit.py::test_a_repeated_interim_does_not_restart_the_hold --junitxml=/private/tmp/fakeyou-next-directive-20261116/pr220-focused.xml
```

## Remaining gates and decisions

- #220 remains draft/open at the reviewed head. Local failure triage and an
  explicit human merge decision remain outstanding. This is not an approval.
- PR B has a separate dependency/file/control/exclusion plan. No implementation
  branch exists. #220 being merged is necessary before branch creation; it is
  not approval to implement every item in the plan without an execution ruling.
- The RustFS report wording now distinguishes the explicit bypass probe from
  ordinary-delete cleanup. The tested alpha.81's Governance and Compliance
  retention failures remain **P0**. No storage probe was rerun.
- Independent custody is proposed separately. The archive's local size/hash
  were rechecked read-only; no deployment, bucket or upload was made.
  **EVIDENCE CUSTODY PENDING** remains in force.
- Original PR #206–#218 and their refs are preserved. #205 is unchanged.
  No external model code review, new provider request, spending, production
  activation, Phase 5B, mark-ready or merge is performed.
