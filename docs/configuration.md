# Configuration

Every setting is an environment variable under the `FAKEYOU_` prefix with `__`
nesting, declared in one schema and validated strictly — an unknown key is a hard
error rather than a silent no-op. Copy `.env.example` and fill it in.

```sh
FAKEYOU_IDENTITY__OWNER_ID=hard_owner
FAKEYOU_IDENTITY__TWIN_ID=hard_twin
FAKEYOU_STT__API_KEY=...            # Muse
FAKEYOU_TTS__API_KEY=...            # Cartesia or MiniMax
FAKEYOU_REPLY__API_KEY=...          # reply model
FAKEYOU_DATABASE__URL=postgresql://fakeyou:...@postgres:5432/fakeyou
FAKEYOU_MEMORY__STORE__PASSWORD=...
```

The twin's replies use up to three model roles, each its own `FAKEYOU_<ROLE>__*` block:

| Role | Used for |
| --- | --- |
| `reply` | Small, fast model: every model call, unless a role below takes it |
| `reply_strong` | Large model for the call that composes an answer from evidence: a memory recall, a closer look at the screen or a camera, a meeting view the twin asked for itself, or memory prefetched for this turn that found facts. The view attached to every call does not count. Leave it empty to answer every turn with `reply` |
| `reply_fallback` | A turn whose model call failed |
| `turn_judge.model` | Deciding, for every line, whether it is meant for the twin and whether the twin answers. Leave it empty to judge with `reply` |

The turn judge reads the recent conversation from the meeting record, the twin's own
lines included, and starts judging on the live transcript while the speaker is still
talking. When it cannot answer in time the twin replies only to a line that names it.

A critic reads every contribution nobody asked for and every opinion or proposal in an
answer, and asks whether the owner would say it, whether it is worth interrupting for,
whether a guess is spoken as a guess, whether it rests on the sources it cites, whether it
adds anything and whether it is on topic. It runs on the `turn_judge.model` role and
waits for its verdict before the words are spoken, so a plain factual answer pays
nothing for it. Its confidence floor is `FAKEYOU_PERCEPTION__ATTENTION__CRITIC_CONFIDENCE`.

The twin's voice can have a fallback too: `FAKEYOU_TTS_FALLBACK__*` takes the same keys as
`FAKEYOU_TTS__*`. A sentence the main provider fails before any audio is spoken by the
fallback instead. When the main provider runs out of credit or rejects the key, the rest of
the session speaks through the fallback; any other fault is tried on the main provider again
at the next sentence. Leave it empty to have no fallback.

The answer to a line that names the twin starts as soon as the final transcript
arrives, while the turn judge is still deciding (`FAKEYOU_CONVERSATION__SPECULATIVE_REPLIES`,
on by default). Nothing is spoken and no reply effect (louder, shorter, language) applies
until the judge admits the turn; a turn the judge silences, or plans differently (a look at
the screen or a camera, a Meet action, a chat reply, leaving, an owner claim), drops the
draft and answers as before. Set it to `false` to start every answer after the judge.

When `reply_strong` is set, the response budget
(`FAKEYOU_CONVERSATION__RESPONSE_TIMEOUT_SECONDS`) is checked against it instead of
`reply`.

A Google role (`provider=google`) thinks at `REASONING_EFFORT=minimal` unless you set
another level. Some Gemini models reject `minimal` (for example `gemini-3.8-flash`, whose
lowest level is `low`), so set `FAKEYOU_<ROLE>__REASONING_EFFORT=low` for them. A
worker checks the models of its role when it starts — `sessions`: reply, strong,
fallback and turn judge; `debrief`: debrief and fallback — and refuses to start on a
level its model does not support, instead of failing every meeting later.

The persona's `ai_disclosure` sets when the twin says it is an AI: `when_asked`
(the default) or `upfront`, the first time it speaks. Either way it answers
honestly when asked and never claims to be human; no value lets it deny being an
AI.

The twin knocks on a meeting once. `FAKEYOU_MEETING__LOBBY_TIMEOUT_SECONDS` is the whole
wait for admission, and a lobby that runs out ends the session as `lobby_timeout_reached`
instead of knocking again. `FAKEYOU_MEETING__JOIN_MAX_ATTEMPTS` and the
`FAKEYOU_MEETING__JOIN_RETRY_*` backoff only cover failures before the knock: opening the
page, typing the name, finding the join button.
