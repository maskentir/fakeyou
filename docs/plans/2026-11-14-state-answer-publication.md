# Phase 5A — State answer publication binding

This follows #198 (`31b1d5a`). The `presenter` and `screen_title` state answers
describe the meeting as it is now, so they were left undeclared in #198 and would
have been prevented under an active publication context. They now carry their own
provenance and a publication binding. Everything here is offline; no publication
context is activated in production, and paid execution stays at zero requests.

## State provenance is not visual evidence

A state answer is rendered from a kernel `Fact`, not from a camera or screen
capture. It has no frame, no snapshot, and no provider attempt. The contract
therefore gains a separate `StateProvenance` instead of reusing `EvidenceBinding`:

| Field | Meaning |
| --- | --- |
| `question` | `presenter` or `screen_title` |
| `fact_id`, `valid_from_seq` | the kernel fact and the session sequence it was derived at |
| `namespace`, `modality` | where the fact came from; see below |
| `domain`, `revision` | the kernel revision domain and its value at derivation |
| `subject_id` | presenter device, or presenter device plus screen epoch for a title |
| `claim` | digest of the proposition the sentence was rendered from |
| `known` | false when the fact is an `Unknown` abstention |
| `source_health` | reported `meet_protocol` health at derivation |

The two answers do not share a source:

| Answer | Namespace and modality | Source |
| --- | --- | --- |
| `presenter` | `meeting_live` / `participant_claimed` | meeting protocol roster, endpoints, display claim |
| `screen_title` | `meeting_live` / `screen_read` | protocol sharing state plus the screen reading |

No `SnapshotRef`, capture time, or provider attempt identity is invented for
either. A sentence binding with the new `state` scope carries state provenance and
must carry no visual evidence. The visual policy never approves a `state` binding.

## Binding and identity

`answer_state` receives a `StateAnswer` holding the rendered text and its
provenance. When the speaker has a publication context, the answer is prepared,
bound, and spoken as a prepared clip. The binding has a fresh publication ID, the
turn request ID, and the hash of the final prepared text. Nothing joins on the
latest screen event or latest state.

Without a publication context the answer is spoken exactly as before. An answer
that reaches playback under a context without provenance stays undeclared direct
speech and is prevented, so a missing provenance fails closed.

## Validation before the first PCM write

`evaluate_state_publication` runs in the existing gate, once per clip, immediately
before its first PCM write. It re-derives the fact from the latest session state:

| Latest state | Decision |
| --- | --- |
| same subject and same claim | valid, even if the revision advanced for an unrelated reason |
| different presenter device or screen epoch | invalid, `state_subject_changed` |
| same subject, different proposition | invalid, `state_changed` |
| source stale, roster unsynced, or question unsupported | unknown, `state_source_unknown` |
| revision or sequence older than the binding | unknown, `state_revision_regressed` |

Invalid and unknown both prevent the clip. Playback does not rewrite or re-answer
the sentence. Playback marks the prepared clip as prevented, and `answer_state`
notes a state answer as spoken only when it was emitted, so a prevented answer
does not advance the actor's spoken-sentence count, quiet-floor activity, or the
spoken observer. A `state` binding with no gate configured is unresolved and is
prevented.

## Abstention

When the fact is `Unknown`, the rendered sentence says the answer cannot be
determined and names no presenter or title. That binding uses the `no_visual`
scope and keeps its provenance with `known` false for traceability. It is allowed
with or without a gate, and stays allowed if the source recovers before playback.
"Nobody is presenting" is a known fact, not an abstention, and is revalidated.

## Measurement

Local telemetry records the state fact ID, revision, and `known` flag alongside the
publication identity. `measure_publication` never reports a `state` publication as
observed compliant: there is no participant-side continuity source, so an allowed
and heard state claim is `not_measurable` with `state_continuity_unmeasurable`. A
vetoed state claim that a valid observation still heard is observed noncompliant.

## Deterministic proof

`tests/test_state_publication.py` drives `answer_state` through a real playback
engine and real kernel state with fake synthesis and clocks.
`tests/test_assembly_state_speech.py` takes the state speech that the production
conversation builder wires into the actor, over a real kernel port.

| Case | Test |
| --- | --- |
| Presenter changes while the clip is queued | `test_state_change_while_queued_prevents_the_old_claim` |
| Screen title changes before the PCM write | same |
| Screen sharing stops | same, both questions |
| Protocol source goes stale | same, both questions |
| Binding with a matching revision is published | `test_presenter_answer_with_matching_revision_is_published_with_its_binding` |
| Unrelated revision bump keeps an unchanged claim | `test_unrelated_screen_revision_keeps_an_unchanged_presenter_claim` |
| Repeated attempts get distinct publication IDs | `test_repeated_state_answers_get_distinct_publication_ids` |
| Unknown-state abstention still speaks | `test_unknown_state_abstention_still_speaks` |
| No silent regression under an active context | the published and abstention tests above |
| Prevented answer is not counted as spoken | `test_actor_counts_a_state_answer_as_spoken_only_when_it_was_emitted`; note assertions in the cases above |
| Production assembly supplies the provenance | `tests/test_assembly_state_speech.py` |
| Non-visual state answers keep carrying none | `test_production_non_visual_state_answers_carry_no_provenance` |
| No fabricated visual evidence | `test_state_provenance_never_fabricates_visual_evidence` |
| No participant compliance claim | `test_participant_observation_of_a_state_claim_is_never_reported_compliant` |

## Not done

No production path builds a `PublicationContext` or composes the state gate with
the visual gate. Activation is a separate gate. The participant audio adapter,
consent source, remote clock mapping, and Phase 5B remain out of scope.
