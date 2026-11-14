# Production readiness 1/5 — Unified playback outcome accounting

Base: `main` at `51b2c85` (#199). Offline only; no publication context is activated
and paid execution stays at zero requests.

## Problem

Callers treated a `speak()` or `speak_prepared()` call that returned without an
exception as proof that the sentence was spoken. `TurnSink.say` noted the
sentence before playback, and `answer_state` noted it after. A clip that the
publication gate prevented, an empty clip, or a clip cancelled before its first
PCM write still advanced the spoken-sentence count, the quiet-floor activity, and
the interjection quota. #199 patched one caller with a flag on the prepared clip.

## Contract

Playback is the only source of truth for what was written to the local output.
It issues an immutable `PlaybackReceipt` per clip attempt:

| Outcome | Meaning |
| --- | --- |
| `attempted` | the clip left the queue; no PCM yet |
| `prevented` | the publication gate refused the clip before its first PCM |
| `dropped_before_audio` | nothing to say, empty audio, stale, cancelled, or failed with no PCM |
| `audio_started` | the first PCM frame of the clip was written |
| `interrupted_after_audio` | the clip stopped after some PCM, by barge-in, timeout, or failure |
| `completed` | every frame of the clip was written |

A receipt carries the attempt ID, the text, the cue flag, the publication ID when
the clip was bound, the transcript ID and sentence index, a detail string, and
`local_audio_seconds`, the duration of clip PCM written so far. One frame of PCM
is `audio_started`, not `completed`; partial playback keeps its partial duration.

Every clip attempt has its own attempt ID. A sentence that is replayed is a new
attempt with its own receipts.

Receipts describe the local output only. A written frame does not establish that
any participant heard it. Participant-side audibility remains unmeasured.

## One consumer path

`PlaybackEngine.observe_receipts` delivers receipts as they are issued, including
when the caller was cancelled. The meeting audio session, the stream fleet, and
the replay harness forward the subscription. `FloorActor.bind` subscribes once.

The actor counts an attempt as spoken the first time a receipt for it reports
local audio, and never again for the same attempt ID. Cue receipts and receipts
after close are ignored. That one rule covers every caller:

| Condition | Spoken count | Spoken observer |
| --- | --- | --- |
| Gate prevents before the first PCM | unchanged | not called |
| Unbound direct speech refused | unchanged | not called |
| Prepare fails, nothing to say, or empty clip | unchanged | not called |
| Cancelled before PCM | unchanged | not called |
| First PCM written | +1 | called once |
| Barge-in after partial PCM | still +1 | not called again |
| Sentence replayed as a new attempt | by the new attempt's outcome | by the new attempt |

`TurnSink.say`, `answer_state`, the recovery apology, and declared no-visual
speech no longer note anything themselves. `TurnContext.note_spoken`, the
`PublicationOutcome` protocol, and the prevented flag on prepared speech are
removed. A speaker that issues no receipts counts nothing.

`SpokenLog` reads the same receipt. The journal row gains the attempt ID, outcome,
and local audio seconds. A prevented clip is no longer written to the meeting
record as interrupted twin speech, and a clip that finished without PCM is not
recorded as spoken. Interruptions keep their existing record behavior.

## Unchanged

The publication gate, its timing before the first PCM, fade-out, barge-in, freeze,
and queue behavior are untouched. The fade ramp written after a cancelled first
frame is not clip audio and does not turn the receipt into started audio. A faded
frame of the clip itself, written when a clip is superseded mid-stream without a
publication context, is clip PCM and is counted in `local_audio_seconds`.

## Deterministic proof

| Case | Test |
| --- | --- |
| Receipt sequence, attempt identity, partial versus complete duration | `tests/test_playback_receipts.py` |
| Gate veto, unbound direct speech, declared no-visual speech | same |
| Nothing to say, empty clip, cancellation before PCM, queued clip stopped | same |
| Barge-in after partial PCM | same |
| Replay as a new attempt; prevented then replayed | same |
| Actor counts once per attempt, ignores cues, close, and rebind | same |
| Record and journal read the receipt | same |
| Visual reply through `TurnSink` counted only when PCM was written | `tests/test_turn_graph_publication.py` |
| State answer counted only when PCM was written | `tests/test_state_publication.py` |
| Interjection quota consumed only by voiced contributions | `tests/test_actor_lanes.py` |
