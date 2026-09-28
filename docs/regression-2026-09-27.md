# Regression Report: Digital Twin Foundation

**Date** 2026-09-27 · **Version** 0.1.0 · **Scope** local static, unit, evaluation, browser smoke, live Muse STT, packaging, and Compose checks

## Summary

| Check | Result | Scope |
| --- | --- | --- |
| `uv run ruff check .` | PASS | 0 lint errors |
| `uv run ruff format --check .` | PASS | 25 files already formatted |
| `uv run mypy` | PASS | 7 source files, 0 issues |
| `DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run pytest` | PASS | 19 passed, browser and live Muse deselected |
| `DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run deepeval test run evals/test_meeting_agent.py` | PASS | 1 passed, 100% pass rate |
| `uv run pytest tests/test_browser.py -m browser` | PASS | 1 passed, 1 non-browser test deselected |
| `uv lock --check` | PASS | Lockfile is current |
| `uv pip check` | PASS | Installed packages compatible |
| `uv build` | PASS | Source distribution and wheel built |
| `docker compose --profile production config --quiet` | PASS | Compose configuration valid |
| Existing Compose service health | PASS | `fakeyou-neo4j-1` healthy; ports loopback-only |
| Graphiti startup lifecycle | PASS | Worker construction, schema readiness, local connectivity, and cleanup |
| Native Pipecat lifecycle | PASS | Audio frame flow, final transcript, shutdown, and upstream error rejection without provider calls |
| Live Muse test with Indonesian synthetic audio | PASS | 9.076-second PCM fixture streamed in real time; final transcription matched |

## Findings

### Fixed: Graphiti tools could run before schema initialization

`chat --memory` now constructs Graphiti outside the active event loop and awaits schema setup before exposing tools. Regression tests cover readiness ordering and cleanup after setup failure. A local Neo4j smoke check exercised the native construction, setup, connectivity, and close path with an offline placeholder model key.

### Limitation: concurrent SQLite writers

A separate stress probe opening 32 native SQLite saver connections against a fresh temporary file produced 5 `database is locked` failures. The CLI remains a local single-writer development interface. The configured PostgreSQL backend still needs CLI integration before concurrent meeting workers. SQLite uses the driver's default lock timeout; it has no project-level write coordination.

### [LOW] Third-party deprecation warnings during pytest

- **Location** installed dependencies (`graphiti_core`, `pipecat`)
- **Detail** Pytest reported three warnings: Pydantic class-based config deprecation in Graphiti, Python `audioop` deprecation, and Pipecat `AudioContextTTSService` deprecation.
- **Impact** No current test failure; future dependency or Python upgrades may require compatibility updates.
- **Confidence** High

## Coverage gaps and limitations

- The live Muse check used the local credential bundle and made one short external STT call. Other checks used no provider credentials and made no external model, TTS, Graphiti retrieval, or Google Meet calls.
- DeepEval validated the deterministic `raise_hand` tool trajectory with its offline metric; it did not judge live model reasoning.
- The browser test exercised the local browser/MCP path and a local page only. It does not prove guest admission, Meet media, or long-running meeting behavior.
- No coverage percentage was collected; this regression run focused on pass/fail behavior and configured smoke paths.
