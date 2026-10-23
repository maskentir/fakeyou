## What changed

<!-- One line per change, named by behaviour, not by file. -->

## Why

<!-- The problem, and where it showed up: a live session, a journal, an eval, a report. -->

## Evidence

<!-- Numbers before and after, with their source (meeting id, eval artifact, probe). Write "none" if there are none. -->

## Gates

- [ ] `uv run --no-sync ruff check .`
- [ ] `uv run --no-sync ruff format --check .`
- [ ] `uv run --no-sync mypy`
- [ ] `uv run --no-sync lint-imports`
- [ ] `uv run --no-sync deptry .`
- [ ] `uv run --no-sync pytest`
- [ ] Live evals, when a model, prompt or provider changed: <!-- which ones and the result -->

## Live verification

- [ ] Not needed: the change is offline only
- [ ] Done in a real Meet: <!-- meeting id, length, terminal reason -->
- [ ] Still needed: <!-- what a live session has to prove -->

## Configuration

- [ ] No `FAKEYOU_*` key added, renamed or removed
- [ ] Changed keys are in `.env.example` and `tests/fixtures/env_migration_d9.json`
- [ ] Operators must update `.env`: <!-- keys and values -->

## House rules

- [ ] No comments or docstrings; names carry the meaning
- [ ] Code, prompts and test data in `.py` files are English; Indonesian lives only in JSON
- [ ] Library features before custom helpers
- [ ] No secrets, `.env` files, recordings or meeting records committed
- [ ] Each commit is one short Conventional Commit subject

## Not done, on purpose

<!-- What this PR leaves out, and why. -->
