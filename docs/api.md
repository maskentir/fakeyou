# API reference

All routes are token-authenticated with `Authorization: Bearer <token>`, using the
owner or the admin token. The endpoint list is in the [README](../README.md#api);
the running API serves interactive documentation at `http://127.0.0.1:8000/docs`.
This page describes the behaviour behind the endpoints.

## Decision register and decision lineage

The decision register lists what meeting debriefs recorded: decision claims,
proposals, owner-decision requests, action items and open questions, each with
its meeting, its quotes and the transcript entries they come from. Filter with
`meeting_id`, `twin_id`, `kind` and `status`, and page with `limit` and
`cursor`. Every record is a meeting claim, `owner_approved: false`, whatever it
says about the owner; approvals live only in the owner model. `attribution` is
`recorded_label` when the meeting record names a speaker (a caption name, a
device, a voice match) and `unattributed` when it does not; a recorded label is
not a verified identity. Two items are separate records when anything in them
differs, quotes, assignee or due text included; only an exact repeat collapses.
`meetings` lists every meeting in scope with its debrief status, so a missing or
stale debrief never reads as a meeting without decisions. A `next_cursor` holds
only for the same filters and the same register: another filter is refused with
`cursor_query_mismatch`, a changed debrief or a forgotten meeting with
`cursor_stale`; start again without a cursor.
Forgetting with `keep_record: true` erases the meeting from derived memory, the
graph, and keeps its record and debrief, so it stays in the register;
`keep_record: false` erases it from the register too.

Decision lineage records how two register records from different meetings of
the same owner and twin were compared: `related_discussion`,
`potential_contradiction`, `potential_supersession` or
`no_established_relationship`. Every comparison is a derived candidate,
`authority: derived_candidate`, `review_status: unreviewed`,
`owner_approved: false`; it never approves, resolves or retires either claim,
and both stay in the register unchanged. `no_established_relationship` means
the recorded comparison found none, not that none exists; a pair nobody
compared counts as `not_compared`. The only source today is a curated
document (`method: manual_curated`) published with
`fakeyou decisions import-lineage --file PATH`; it names the later meeting, and
each comparison names an earlier and a later record. The import is refused,
and nothing is written, when a record is unknown or belongs to another owner,
when the two records share a meeting or a twin differs, when a pair repeats or
carries two labels, or when the meetings' `created_at` times do not put the
earlier one strictly first; meeting ids never decide the order. The earlier
record must sit inside the comparison window: the 20 most recent meetings of
the same twin created strictly before the later one with an available debrief,
newest first, then their records in that order up to 200. The document is
validated, bound to a fingerprint of each record's item, quotes, source entries
and meeting time, and written atomically as `decision-lineage.json` beside the
later meeting's debrief. Validation and publication run while holding the
meeting's record lock (`purpose: lineage`), the same lock forget holds: an
import during a forget, a debrief or another import is refused with
`decision_lineage.locked` (409) and writes nothing; a forget during an import is
refused the same way and can be repeated; an import whose meeting was forgotten
first finds no meeting (404) or no usable debrief (`lineage_source_unavailable`)
and never recreates its folder. Each published file has a `revision`, the
first 32 hex characters of the SHA-256 of its bytes, returned by the import and
shown per meeting as `lineage_revision`. Importing the same comparisons again,
in any order, returns `outcome: unchanged` and leaves the file and its
`imported_at` alone. A different document is refused with
`decision_lineage.exists` (409) unless it carries `replaces` set to the current
revision, which yields `outcome: replaced`; any other `replaces` is refused with
`decision_lineage.revision_stale` (409), so of two replacements made from the
same revision only the first lands. An unreadable file has a revision too and is
replaced the same way. A failed write leaves the previous file in place.
`fakeyou decisions lineage RECORD_ID` shows both incoming comparisons (from
earlier meetings) and outgoing ones (to later meetings) with the other record's
full evidence. A comparison whose record was forgotten, changed or left without
an available debrief counts as `unresolved`; one whose record kept its text but
changed its sources or meeting time counts as `invalidated`. Neither is moved to
a similar record. A malformed or altered `decision-lineage.json` serves nothing
and reads as `invalid` in `meetings`. An unknown, forgotten or other owner's
record id answers the same 404. Forgetting the later meeting erases its lineage
with its record.

## Owner model and persona

The owner model is what the twin may stand behind on the owner's behalf:
positions, preferences, decision rules, style examples, and facts. Owner notes
are owner model facts: a `fact` item with an optional `valid_at`, the moment it
became true. Every change is an append-only revision in PostgreSQL, so earlier
versions stay readable.
Items the owner writes are active at once and bump the model version; items
learned from a debrief arrive as proposals and change nothing until approved.
`owner_only` items are for the owner's eyes and never spoken in a room.
Retiring an item takes it out of the model and keeps its history; forgetting
erases the content of every revision and leaves only a contentless marker, so
model versions never repeat.
The persona holds only who the twin is and how it speaks: `display_name`,
`owner_name`, `role`, `language`, `speaking_style`, `vocabulary`, `ai_disclosure`,
and `allowed_tools`. People in a meeting call the twin by the owner's name, so a
line that opens with `owner_name` ("Rani, can we…", "Hi Rani, …") addresses the
twin just like one naming `display_name` anywhere; a mention elsewhere in the
line ("ask Rani later") does not. What the twin knows about the owner lives in the owner model, so
a persona that still carries `verified_facts` or `goals` is refused with
`persona.knowledge_moved`. `fakeyou owner-model import --persona PATH` moves them
once: each `verified_facts` line becomes a `fact` and each `goals` line a
`preference`, word for word, approved with source `import`; a statement the model
already holds with the same kind is skipped, so running it again adds nothing.
Turn an imported fact that is really a rule into a `decision_rule` with
`owner-model revise`, then delete both keys from the persona file.
`fakeyou brief` drafts from the `room_speakable` items too.
Each session freezes the model version it starts with: the twin answers from
the `room_speakable` items, cites them as `om-<item>`, and a `brief_only`
meeting uses none of them. Forgetting an item does not reach sessions that
already froze it: their records keep what they ran with until the meeting
itself is forgotten with `memory-forget --meeting-id`.
