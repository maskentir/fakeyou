# Production readiness 4/5 — Opt-in gate composition and activation readiness

Base: `feat/participant-audio-measurement` (3/5). Offline only. Nothing in
production builds a publication context after this PR. A test asserts that no
production module imports the composition, and that both audio paths default to
no context.

## Composition

`compose_publication_gate` is one gate for every bound sentence:

| Scope | Decision |
| --- | --- |
| `no_visual`, `historical` | valid, as before |
| `state` | `state_publication_decision` against the latest kernel state; unresolved without a state source |
| `current` | `evaluate_visual_publication` with the scene revalidator; unresolved without one |

`opt_in_publication_context` builds the context from a perception context and a
kernel port, on the `local_monotonic` clock domain that the capture mapping from
2/5 uses. A caller has to construct it and pass it in; there is no setting.

`SceneRevalidator` answers from perception state without a provider call:

- The current scene is the current screen segment, or the participant's live
  camera frame under the existing liveness rule.
- `unchanged` is whether the bound snapshot and the current scene share source,
  subject, stream, epoch, and generation.
- `supported` is true only when the bound look produced an answer. It cannot
  re-infer the claim, so for an abstaining or failed look it reports unknown.

The evidence binding gains `status`, the outcome of the look it came from.

The stream fleet could not carry a publication context. `SlotFactories` now has an
optional `publication`, default none, passed to every slot.

## Activation findings

These are behaviors of the approved contracts under realistic timing. They are
proven by tests named `test_finding_*` and are not changed here.

1. **A static slide is prevented.** A screen keyframe keeps the capture time of
   the first frame of that slide; later identical samples do not refresh it. A
   claim about a slide that has been up for more than 5 s is `capture_too_old`.
   Past 120 s the capture instant is outside the mapping span and is
   `capture_time_unmapped`. With the gate active, most screen answers in a normal
   presentation would be silent.
2. **A slow reply is prevented.** A claim written more than 5 s after the frame
   was captured is `capture_too_old`, whatever the scene did. The readiness audit
   of 2026-10-22 measured 6 to 9 s reply latency in live sessions.
3. **A reply after an abstaining look is prevented.** When the look returns
   `not_visible` or `unclear`, the reply is usually an abstention, but it is bound
   to the same evidence with the `current` scope, the revalidator cannot report
   support, and the gate answers `validation_support_unknown`. "I cannot see that
   on the screen" would be silent.
4. **Camera scene identity does not see content.** A new camera frame from the
   same stream and generation is the same scene. The gate confirms liveness and
   identity, not that the person still looks the same.

Findings 1 to 3 mean activation would cause silent regressions on correct
answers. They need a contract decision before activation, for example how a
confirmed-identical later sample relates to capture age, and which scope an
abstention after an insufficient look carries. No such decision is made here.

## What passes

Under the composed gate with real perception state, a real kernel port, a real
playback engine, and a real actor: a fresh supported screen claim and a fresh
camera claim are published and counted once; a changed screen, stopped share, lost
camera, unmapped capture clock, and changed presenter are prevented and not
counted; a reply without visual needs, a state answer, and the declared no-visual
greeting are published.

A published claim measured without a participant observer is `not_measurable`
with `participant_observer_unavailable`.

## Deterministic proof

`tests/test_publication_activation.py` (23 tests), covering every row above, the
gate dispatch without sources, the fleet seam, and the two guards that keep the
context off in production.
