# Production readiness 5/5 — Offline regression and negative-control audit

Base: `feat/publication-gate-composition` (4/5). Offline only.

## The stack

| # | Branch | Subject | Depends on |
| --- | --- | --- | --- |
| 1 | `feat/playback-outcome-accounting` | unified playback outcome receipts | `main` at `51b2c85` |
| 2 | `feat/evidence-capture-provenance` | measured capture clock mapping, provider attempt identity | 1 |
| 3 | `feat/participant-audio-measurement` | consent-aware participant audio contract, fake adapter | 2 |
| 4 | `feat/publication-gate-composition` | opt-in gate composition, activation readiness | 3 |
| 5 | `test/readiness-regression-audit` | this audit | 4 |

Each PR is a draft, has its own design note under `docs/plans/2026-11-15-*`, and
is reviewable on its own diff against the PR below it. Nothing is merged.

## Negative controls

`tests/test_publication_audit.py` asks one question in three ways: can anything
short of complete evidence produce a compliant verdict?

1. **Exhaustive property.** 2,000 derandomized combinations of gate decision,
   playback receipt, observer presence, consent state, scope, source, grant and
   revocation times, identity method, observer clock, report, sentence identity,
   capture mapping, provider identity, and post-speech continuity. The verdict is
   `observed_compliant` exactly when every piece is present and valid. Any
   `observed_*` verdict requires a usable consented observation. Any `prevented_*`
   verdict requires a veto and a no-write receipt. A participant gap never
   coexists with a compliance value.
2. **One removal at a time.** Starting from the fully evidenced control, each of
   23 single removals yields its own named status and reason, none compliant.
3. **End to end.** A real actor, playback engine, kernel port, perception
   timeline, composed gate, and playback receipt, with the scripted observer. The
   control reaches compliant only with every fake in place. Without an observer,
   which is the only production case, the result is `not_measurable` with
   `participant_observer_unavailable`.

The property found one hole, fixed here: an allowed sentence whose playback
receipt says no audio was written, but which a participant reports hearing, was
reported compliant. It is now `publication_observation_conflict`, the same rule
the veto path already had.

## Regression

- Every speech path (visual, state, non-visual state, reply without visual needs)
  counts exactly the attempts with local audio, has one attempt ID, and settles as
  `completed` or `prevented`.
- With no publication context, which is the production default, every one of
  those turns is spoken, nothing is bound, and no receipt carries a publication
  ID, including the static-slide and changed-screen cases the gate would prevent.

## Preservation

`git diff 51b2c85..HEAD` is empty for `evals/fixtures` (cassettes, datasets),
`tests/fixtures`, `docs/reports` (historical ledgers and scorecards),
`src/fakeyou/observability/scorecard.py` and `report_contract.py` (SLOs),
`src/fakeyou/config` (settings and defaults), `README.md`, `pyproject.toml`,
`uv.lock`, the package manifests, and the deployment files. No `FAKEYOU_*` key is
added. No provider was called; cost is US$0.

Changed outside `src/` and new tests: `evals/_conversation_replay.py` and
`tests/_audio_fakes.py` issue simulated playback receipts, because a speaker that
issues none now counts nothing as spoken.

## Gaps that remain

None of these is closed by this sprint, and none is papered over.

| Gap | Effect today |
| --- | --- |
| No real participant audio observer, consent capture, or identity matcher | every production measurement is `participant_observer_unavailable` |
| No measured clock exchange with a participant device | participant timing cannot be mapped |
| The page clock exchange has not run against a real Meet page | live capture mapping quality is unknown |
| Static slide older than 5 s is `capture_too_old` | activation would silence most screen answers |
| Reply slower than 5 s after capture is `capture_too_old` | activation would silence slow correct answers |
| Reply after an abstaining look is `validation_support_unknown` | activation would silence visual abstentions |
| Camera scene identity ignores content | the gate proves liveness and identity only |
| Sentence-level claim support is not established | the whole reply's evidence is bound to each sentence |
| Local PCM write is not audibility | no receipt says a participant heard anything |

Activation of the publication context is not ready. Phase 5B is not authorized
and nothing here prepares a paid run.
