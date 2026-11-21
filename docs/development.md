# Development

## Quality gates

Every pull request runs these gates in CI, and they run the same way locally:

```sh
uv run --no-sync pytest
uv run --no-sync mypy
uv run --no-sync ruff check . && uv run --no-sync ruff format --check .
uv run --no-sync lint-imports
uv run --no-sync deptry .
```

`pytest` deselects the `browser`, `live` and `container` markers by default. The
real-service suites run with `uv run --no-sync pytest -m "container or postgres"`
after building the worker image with `docker compose build worker-sessions`.

## Meeting simulator

The meeting simulator runs the whole worker — browser, PulseAudio, speech to text, the
judge, the reply model, text to speech — against a fake Meet page instead of a live
meeting. It plays each participant through the same boundaries a real call uses, then
checks the twin's journal. It needs the audio test image and, by default, no API keys:

```sh
docker build -f docker/audio/Dockerfile --target test -t fakeyou-test:local .
docker run --rm -v "$PWD/tests:/workspace/tests:ro" -v "$PWD/evals:/workspace/evals:ro" \
  -v "$PWD/src:/workspace/src:ro" -v "$PWD/conftest.py:/workspace/conftest.py:ro" \
  -v "$PWD/pyproject.toml:/workspace/pyproject.toml:ro" \
  fakeyou-test:local uv run --no-sync pytest -m simulator evals/test_meeting_simulator_eval.py
```

The modes that reach a provider also need `-v "$PWD/.env:/workspace/.env:ro"`.

`SIMULATOR_PROVIDERS` picks where the providers come from:

- `replay` (the default) needs no `.env` and contacts no provider. It rebuilds the settings from
  the recorded snapshot, replays Muse and Gemini from the cassettes in
  `evals/fixtures/simulator/cassettes/`, and speaks through a synthetic Cartesia. CI
  runs it on every pull request, with every provider host mapped to a refused address.
  Every provider endpoint, the memory model included, points at the doubles, and a
  request to a route no double serves fails the scenario.
- `live` calls the real providers and spends money; select it explicitly.
- `record` relays Gemini and Muse to the real services and rewrites the cassettes and
  the settings snapshot.
- `record_missing` replays what a cassette holds and relays to Gemini only the requests
  it does not hold, appending them. It needs the Gemini keys in `.env` and a recording
  budget.

**Replay contract** (`evals/_replay_cassette.py`). A Gemini request matches a recording
only when its canonical identity is identical:

- the route, which carries the model and the method
- the whole JSON body: system instruction, every turn, generation config and tools
- images replaced by the SHA-256 of their bytes
- two measured values replaced by a placeholder, because replay timing cannot
  reproduce them: `vision_latency_ms` and the whole seconds in `seconds_ago`

Identical requests are served in the order they were recorded. A request with no
identical recording is answered with a provider error that no client retries (HTTP 400),
never with another recording, so asking it again is a repeated request.
Each spoken clip plays its recorded takes in order.

A scenario fails unless its replay is closed:

- no unrecorded, repeated or malformed request
- no recording left unused
- no clip heard more often than it was recorded

The only exception is an unrecorded or unused turn-judge prefetch for interim text that
never became a final transcript. Whether the worker sends one depends on scheduling,
not on the conversation, so it is reported as speculative and does not fail the
scenario. A request counts as a judge prefetch only when it has the production judge's
shape: the judge model's `generateContent` route, and a body that equals the recorded
turn-judge requests in everything but its one user turn (system instruction, the whole
response schema, generation and safety settings, no extra field), with that turn holding
exactly the judge window. The cassettes must agree on one such frame, and a test checks
it against a request the worker builds today. A repeated request is
fatal even when it is a judge prefetch.

Each scenario writes `reports/<scenario>.json` under `SIMULATOR_ARTIFACT_DIR`, holding:

- the revision and the fixture digests
- the simulator's actions and the journal timeline
- every replay miss, with the closest recording's differing lines
- a failure category: environment, browser, protocol, provider replay, audio, policy,
  behaviour, timing or cleanup

To record after changing a prompt, a setting or a scenario, mount the cassette folder
writable:

```sh
docker run --rm -e SIMULATOR_PROVIDERS=record \
  -v "$PWD/evals/fixtures/simulator/cassettes:/workspace/evals/fixtures/simulator/cassettes" \
  ...same mounts as above... fakeyou-test:local uv run --no-sync pytest -m simulator evals/test_meeting_simulator_eval.py
```

`record` and `record_missing` refuse to start without `SIMULATOR_RECORDING_BUDGET`,
the path of a writable JSON ledger (`evals/_recording_budget.py`) holding:

- caps: upstream attempts, estimated spend in USD, and the model's output-token limit
- the verified price per model

Before each upstream call:

- the worst case (request bytes as input tokens plus the full output limit) is reserved
- a call that would exceed a cap waits for calls in flight, or stops the batch
- a model without a price is never forwarded

After each call, the usage metadata replaces the reservation. A failed call, or one
without usage metadata, is charged its full reservation and stops the batch. A stopped
batch answers every later request with a non-retried error. The ledger keeps every
attempt, its tokens, its cost and the stop reason, and carries them over to the next
scenario run that uses the same file.

`record_missing` replays speech. A new spoken clip has no recording to replay, so it
can be recorded live by naming it in `SIMULATOR_RECORD_SPEECH` (for example
`review-token.wav`). The run's speech session is then relayed to Muse
(`evals/_speech_recording.py`):

- the session handshake must name `muse-voice-transcribe-1.0` and 16 kHz 16-bit PCM
- the live Muse key from `.env` replaces the worker's token; the key is never written
- every caught clip must be the named clip, heard once (take 1)

The run stops on any of:

- a second session
- more than 10 minutes of audio
- a 15-minute wall clock
- a provider error or an unexpected close
- a take without `speechStart`, a transcript, `speechComplete` with text, and the voice-end
  marker

A stopped run leaves the cassette exactly as it was. Takes that were not heard are dropped.
The report's `speech_recording` gives the session count, PCM bytes, audio seconds, wall
seconds, close code, recorded take, estimated charge (USD 3 per 1,000 audio minutes) and
stop reason.

Then run `replay` until every scenario is closed. Where a replay asks something the
recording run did not, fill the gap with `record_missing`, and replay again.
`record_missing` rewrites the cassette to exactly what its run used and added, so a
recording that no run asks for any more is dropped.

Meta-tests enforce the conventions mechanically rather than by review: no comments
or docstrings in source, no Indonesian in source files, every test file named after
the module it covers, every error code constructed somewhere, `print` confined to
the output module, and no bare integer exit codes.

Live evals that grade replies with a judge model need their own judge: set
`EVAL_JUDGE_API_KEY`, `EVAL_JUDGE_PROVIDER` (`openai` or `gemini`), and optionally
`EVAL_JUDGE_MODEL` and `EVAL_JUDGE_BASE_URL` for an OpenAI-compatible endpoint. The judge
never borrows the reply model's key, and the persona eval fails when the judge is the
reply model it is grading.
