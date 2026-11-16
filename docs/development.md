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
checks the twin's journal. It needs the audio test image and the API keys in `.env`:

```sh
docker build -f docker/audio/Dockerfile --target test -t fakeyou-test:local .
docker run --rm -v "$PWD/tests:/workspace/tests:ro" -v "$PWD/evals:/workspace/evals:ro" \
  -v "$PWD/src:/workspace/src:ro" -v "$PWD/conftest.py:/workspace/conftest.py:ro" \
  -v "$PWD/pyproject.toml:/workspace/pyproject.toml:ro" -v "$PWD/.env:/workspace/.env:ro" \
  fakeyou-test:local uv run --no-sync pytest -m simulator evals/test_meeting_simulator_eval.py
```

`SIMULATOR_PROVIDERS` picks where the providers come from. `live` (the default) calls
the real ones. `replay` needs no `.env` and no network: it rebuilds the settings from
the recorded snapshot, answers every Gemini call and replays what Muse heard for each
clip from the cassettes in `evals/fixtures/simulator/cassettes/`, and speaks through a
synthetic Cartesia. A request with no close enough cassette entry fails the scenario
instead of passing silently. CI runs `replay` on every pull request. `record` relays
Gemini and Muse to the real services and rewrites the cassettes and the settings
snapshot; record after changing a prompt, a setting or a scenario, with the cassette
folder mounted writable:

```sh
docker run --rm -e SIMULATOR_PROVIDERS=record \
  -v "$PWD/evals/fixtures/simulator/cassettes:/workspace/evals/fixtures/simulator/cassettes" \
  ...same mounts as above... fakeyou-test:local uv run --no-sync pytest -m simulator evals/test_meeting_simulator_eval.py
```

Meta-tests enforce the conventions mechanically rather than by review: no comments
or docstrings in source, no Indonesian in source files, every test file named after
the module it covers, every error code constructed somewhere, `print` confined to
the output module, and no bare integer exit codes.

Live evals that grade replies with a judge model need their own judge: set
`EVAL_JUDGE_API_KEY`, `EVAL_JUDGE_PROVIDER` (`openai` or `gemini`), and optionally
`EVAL_JUDGE_MODEL` and `EVAL_JUDGE_BASE_URL` for an OpenAI-compatible endpoint. The judge
never borrows the reply model's key, and the persona eval fails when the judge is the
reply model it is grading.
