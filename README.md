<p align="center">
  <img src="./assets/banner.svg" alt="Fakeyou: the AI digital twin for engineering teams" width="100%">
</p>

<p align="center">
  <a href="https://github.com/maskentir/fakeyou/actions/workflows/check.yml"><img src="https://github.com/maskentir/fakeyou/actions/workflows/check.yml/badge.svg?branch=main" alt="Foundation checks"></a>
  <a href="https://github.com/maskentir/fakeyou/actions/workflows/simulator.yml"><img src="https://github.com/maskentir/fakeyou/actions/workflows/simulator.yml/badge.svg?branch=main" alt="Meeting simulator"></a>
  <a href="#license"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/python-3.12-blue" alt="Python 3.12">
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#api">API</a> ·
  <a href="#cli">CLI</a> ·
  <a href="docs/configuration.md">Configuration</a> ·
  <a href="#contributing">Contributing</a>
</p>

Fakeyou is a self-hosted AI digital twin for engineering teams. It joins a Google
Meet call as a named participant, transcribes every speaker in real time, answers
when it is addressed and stays quiet when it is not. After the meeting it turns what
it heard into a memory graph and an evidence-backed decision register that the next
meeting can draw on.

Fakeyou runs on your own infrastructure and is licensed under Apache-2.0. It calls
only the speech and model providers you configure.

> [!NOTE]
> Fakeyou is under active development. It supports Google Meet only. See
> [Project status](#project-status) for what is measured and what is not.

## Why Fakeyou

Most meeting tools stop at transcripts and summaries. Transcription libraries read
what was said but never attend the call or decide when to speak. Note-taking tools
hold knowledge but were never in the room, so every answer is reconstructed from
someone else's summary.

Fakeyou starts from the room. The twin is a participant with a persona, a memory of
earlier meetings and a judge that decides whether a line is meant for it. Recall,
debriefs and the decision register all come from having been there, and every claim
keeps a path back to the words that support it.

## Features

| Capability | What it does |
| --- | --- |
| **Attends** | Joins Google Meet headful through CloakBrowser as a named guest, raises and lowers its hand, and posts to chat |
| **Listens** | Transcribes with speaker diarization and removes its own playback from the transcript |
| **Decides** | A turn judge reads every final line against the persona, so the twin answers when addressed and stays quiet otherwise |
| **Speaks** | Streams speech sentence by sentence; barge-in interrupts it cleanly |
| **Remembers** | Builds a Graphiti memory graph per owner and twin from each meeting record |
| **Recalls** | Fetches evidence from memory before the reply model is called |
| **Reports** | Writes source-backed debriefs in background workers, outside the call |
| **Tracks decisions** | Lists decision claims across meetings with their quotes, and links related, contradicting or superseding claims through curated lineage. Meeting claims are never treated as owner approvals |
| **Forgets** | Erases a meeting and everything derived from it, or only the derived memory |

## Quickstart

Requires Docker, [`uv`](https://docs.astral.sh/uv/), Node.js 22 or newer, and
Python 3.12.

```sh
git clone https://github.com/maskentir/fakeyou.git && cd fakeyou
uv sync --locked --all-groups
npm ci --ignore-scripts
cp .env.example .env          # then fill in the credential block
docker compose --profile audio up -d postgres redis neo4j embeddings rustfs
```

Create the object storage bucket once, before the first session:

```sh
fakeyou storage init
```

Start the API and one worker per role:

```sh
docker compose up -d api worker-sessions worker-ingestion worker-debrief worker-reembed
```

Register a meeting and send the twin in:

```sh
export API_TOKEN=$(grep FAKEYOU_API__OWNER_TOKEN .env | cut -d= -f2)

curl -X POST http://127.0.0.1:8000/v1/sessions \
  -H "Authorization: Bearer $API_TOKEN" -H 'content-type: application/json' \
  -d '{"link":"https://meet.google.com/abc-defg-hij","meeting_id":"standup-1",
       "owner_id":"hard_owner","twin_id":"hard_twin","twin_name":"Fakeyou Twin",
       "audio":true,"conversation":true,"memory":true,
       "persona":{"owner_id":"hard_owner","twin_id":"hard_twin",
                  "display_name":"Fakeyou Twin","role":"Project coordinator",
                  "language":"en","allowed_tools":["recall_meeting_context"],
                  "ingest_transcripts":true}}'

curl -X POST http://127.0.0.1:8000/v1/sessions/standup-1/join \
  -H "Authorization: Bearer $API_TOKEN" -H 'content-type: application/json' -d '{}'
```

Follow the session, then read its report:

```sh
curl -N http://127.0.0.1:8000/v1/sessions/standup-1/events \
  -H "Authorization: Bearer $API_TOKEN"

curl http://127.0.0.1:8000/v1/sessions/standup-1/report \
  -H "Authorization: Bearer $API_TOKEN"
```

Stop the twin at any time:

```sh
curl -X POST http://127.0.0.1:8000/v1/sessions/standup-1/stop \
  -H "Authorization: Bearer $API_TOKEN" -H 'content-type: application/json' -d '{}'
```

## Architecture

<p align="center">
  <img src="./assets/architecture.svg" alt="Fakeyou architecture: the operator calls the api, which hands work to four worker roles over Redis streams; the workers share PostgreSQL, Neo4j with Graphiti, the meeting records, RustFS and an embeddings service" width="100%">
</p>

Fakeyou is one API gateway and four worker roles. Each role is a separate process
with its own resource budget, so a slow debrief cannot affect a live meeting, and
each service restarts on its own (`restart: unless-stopped`).

| Role | Owns | Scales by |
| --- | --- | --- |
| `worker-sessions` | One meeting at a time: browser, audio, turn judge, replies | Adding replicas, one meeting each |
| `worker-ingestion` | Meeting record to memory graph episodes | Adding replicas |
| `worker-debrief` | Meeting record to source-backed debrief | Adding replicas |
| `worker-reembed` | Memory graph embedding refresh | Adding replicas |

The package layering is enforced by `import-linter`, which fails the build if a
package imports one above it. The CLI is a thin HTTP client over the API and holds no
meeting logic of its own.

## API

All routes are token-authenticated with `Authorization: Bearer <token>`, using the
owner or the admin token. The running API serves interactive documentation at
`http://127.0.0.1:8000/docs`. The behaviour behind the decision, lineage and owner
model routes is described in [docs/api.md](docs/api.md).

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `POST` | `/v1/sessions` | Register a meeting and its persona |
| `POST` | `/v1/sessions/{id}/join` | Send the twin in |
| `POST` | `/v1/sessions/{id}/stop` | Stop the twin |
| `GET` | `/v1/sessions/{id}` | Current phase |
| `GET` | `/v1/sessions/{id}/events` | Live event stream |
| `GET` | `/v1/sessions/{id}/report` | Session report |
| `POST` | `/v1/sessions/{id}/ingestions` | Queue memory ingestion |
| `GET` | `/v1/sessions/{id}/ingestion` | The meeting's latest ingestion outcome |
| `GET` | `/v1/jobs/{id}` | One ingestion job's outcome, `pending` until it settles |
| `POST` | `/v1/sessions/{id}/debriefs` | Queue a debrief |
| `GET` | `/v1/sessions/{id}/debrief` | Debrief status and report |
| `GET` | `/v1/decisions` | Decision register across meetings, with quotes |
| `GET` | `/v1/decisions/{record}/lineage` | One record's compared relationships, with both evidence trails |
| `PUT` | `/v1/decisions/lineage/{id}` | Publish a curated `decision_lineage/v1` document for one meeting |
| `POST` | `/v1/twins/{id}/reembeddings` | Queue an embedding refresh |
| `GET` | `/v1/twins/{id}/owner-model` | Active owner model, or `?version=` |
| `POST` | `/v1/twins/{id}/owner-model` | Add an item |
| `POST` | `/v1/twins/{id}/owner-model/imports` | Import persona facts and preferences once |
| `POST` | `/v1/twins/{id}/owner-model/items/{item}/revisions` | Revise an item |
| `POST` | `/v1/twins/{id}/owner-model/items/{item}/retire` | Retire an item, keeping its history |
| `DELETE` | `/v1/twins/{id}/owner-model/items/{item}` | Forget an item and erase its content |
| `GET` | `/v1/twins/{id}/owner-model/items/{item}` | Revision history |
| `GET` | `/v1/twins/{id}/owner-model/proposals` | Learned items awaiting a decision |
| `POST` | `/v1/twins/{id}/owner-model/proposals/{revision}/approve` | Approve, or `/reject` |
| `POST` | `/v1/sessions/{id}/forget` | Erase a meeting; `keep_record: true` erases derived memory only |
| `POST` | `/v1/admin/memory-init` | Create Graphiti indexes |
| `GET` | `/v1/admin/check` | Effective configuration, with secrets masked |

## CLI

The `fakeyou` command is a client of the same API. It reads the owner token from
`FAKEYOU_API_TOKEN` and, for operator routes, the admin token from
`FAKEYOU_API_ADMIN_TOKEN`.

```sh
export FAKEYOU_API_TOKEN=$(grep FAKEYOU_API__OWNER_TOKEN .env | cut -d= -f2)
export FAKEYOU_API_ADMIN_TOKEN=$(grep FAKEYOU_API__ADMIN_TOKEN .env | cut -d= -f2)

fakeyou check
fakeyou memory-init
fakeyou join https://meet.google.com/abc-defg-hij --meeting-id standup-1 --audio --converse
fakeyou report --meeting-id standup-1
fakeyou debrief --meeting-id standup-1
fakeyou brief standup-1 --persona .runtime/audio/persona.json
fakeyou memory-ingest --meeting-id standup-1
fakeyou memory-forget --meeting-id standup-1
fakeyou memory-reembed
fakeyou owner-model import --persona .runtime/audio/persona.json
fakeyou owner-model add position release "Ship the beta after the load test passes" --persona .runtime/audio/persona.json
fakeyou owner-model add fact acme-renewal "The Acme renewal closes on 3 March" --valid-at 2026-02-01T00:00:00+00:00 --persona .runtime/audio/persona.json
fakeyou owner-model revise ITEM_ID fact acme-renewal "The Acme renewal closes on 28 February" --valid-at 2026-02-10T00:00:00+00:00 --persona .runtime/audio/persona.json
fakeyou owner-model retire ITEM_ID --persona .runtime/audio/persona.json
fakeyou owner-model forget ITEM_ID --persona .runtime/audio/persona.json
fakeyou owner-model show --persona .runtime/audio/persona.json
fakeyou owner-model proposals --persona .runtime/audio/persona.json
fakeyou decisions import-lineage --file lineage.json
fakeyou decisions lineage RECORD_ID
fakeyou worker start https://meet.google.com/abc-defg-hij --meeting-id standup-1 --audio
fakeyou worker status standup-1
fakeyou worker stop standup-1
fakeyou diarize run --meeting-id standup-1
fakeyou diarize show --meeting-id standup-1
fakeyou audio purge-expired --days 30
fakeyou perception purge-expired --days 30
fakeyou db upgrade
fakeyou db status
fakeyou storage init
```

Some commands run on the host and are deliberately not API routes, because they
touch the migration path, the operator's filesystem or a local ONNX model:
`db upgrade`, `storage init`, the `purge-expired` retention sweeps, `diarize show`,
`calibrate` and `voiceprint`.

## Configuration

Every setting is an environment variable with the `FAKEYOU_` prefix and `__` for
nesting, declared in one schema and validated strictly: an unknown key is an error,
not a silent no-op. Copy `.env.example` to `.env` and fill it in.

```sh
FAKEYOU_IDENTITY__OWNER_ID=hard_owner
FAKEYOU_IDENTITY__TWIN_ID=hard_twin
FAKEYOU_STT__API_KEY=...            # Muse
FAKEYOU_TTS__API_KEY=...            # Cartesia or MiniMax
FAKEYOU_REPLY__API_KEY=...          # reply model
FAKEYOU_DATABASE__URL=postgresql://fakeyou:...@postgres:5432/fakeyou
FAKEYOU_MEMORY__STORE__PASSWORD=...
```

Model roles, the turn judge and critic, speech fallback, speculative replies,
reasoning effort and meeting admission are described in
[docs/configuration.md](docs/configuration.md).

## Development

```sh
uv run --no-sync pytest
uv run --no-sync mypy
uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
uv run --no-sync lint-imports
uv run --no-sync deptry .
```

CI runs these gates and a replay of the meeting simulator on every pull request.
The simulator, the real-service suites and the live evals are described in
[docs/development.md](docs/development.md).

## Project status

**Production path:** join, listen, decide, speak, barge-in, stop, recall across
meetings, owner model, forget, ingestion, debrief, re-embedding, and the decision
register with curated lineage.

Measured against real Google Meet sessions, not fixtures:

| Measurement | Result |
| --- | --- |
| Join to admitted | 28–49 s, with audio ready at admission |
| Turn latency, first audio out | p50 0.87 s, p95 2.46 s (target ≤ 3 s / ≤ 6 s) |
| Longest meeting | 2 h 16 m, 4 speech-to-text reconnects recovered, 0 synthesis failures |
| Cross-meeting recall | A fact spoken in one meeting returned intact in the next, with its episode id and source label |
| Operator stop | `leaving` in 2 s, `ended` with `stopped_by_owner` in 6 s |

**Known limits:**

- Google Meet only. Teams and Zoom are not implemented.
- An operator stop is the only end-of-session path measured against criteria;
  duration and host-removal endings are exercised but not measured.
- The participation decider and per-stream capture are wired and tested but off in
  the reference deployment.
- A Meet server-side overload ends the session instead of reconnecting
  indefinitely. It is classified, logged and recoverable by restarting the session.

## Roadmap

Fakeyou is growing from a meeting participant into an engineering chief of staff:
a trustworthy history of what teams decided and why.

- **Decision memory:** evidence-backed decision claims across meetings, kept
  separate from what the owner has approved.
- **Decision lineage:** relationships between decisions over time. Classification
  by a model is being evaluated offline against a curated benchmark before it is
  enabled.
- **Controlled participation:** clear boundaries for when the twin may speak for
  its owner.

## Contributing

Issues and pull requests are welcome. Please open an issue to discuss a larger
change before starting on it.

1. Use the issue templates for bug reports, feature requests and live session reports.
2. Keep each pull request focused, and fill in
   [the pull request template](.github/pull_request_template.md).
3. Make sure the [development gates](#development) pass before asking for review.

## License

Fakeyou is licensed under the Apache License 2.0.
