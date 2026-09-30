# Persona and Scoped Memory Access Implementation Plan

> **For agentic workers:** Use `executing-plans` or `subagent-driven-development` to implement and review each task. Checkboxes track completion.

**Goal:** Give the meeting twin a validated owner profile and read-only access to that owner's memory namespace.

**Architecture:** Pydantic validates a JSON persona document. The existing composition root injects its prompt and filters native Graphiti tools before constructing the LangGraph agent. Conversation scheduling, audio transport, and browser lifecycle retain their existing contracts.

**Tech Stack:** Python 3.12, Pydantic 2, LangChain 1.4.2, LangGraph 1.2.12, Graphiti 0.30.2, Pipecat 1.12.0, pytest, DeepEval.

**Spec:** [Project status, Stage 4](../../project-status.md), with [conversation contracts](../../conversation-plan.md) as the integration boundary.

## Global constraints

- Read the installed library documentation/source before editing integrations; prefer native APIs.
- Readable Python without comments/docstrings, strict typing, and no additional orchestration framework.
- One browser/audio worker per meeting; preserve guest host admission and normal stop behavior.
- Keep credentials in the existing ignored `.env`. Persona documents contain no provider keys.
- Use native `create_meeting_agent(..., persona=...)`, native Graphiti `group_id`, and native Pydantic validation.
- Do not infer the owner from a transcript speaker id, and do not merge another owner's namespace.
- Persona is configuration, not permission enforcement. Enforce tool availability when composing the agent.
- This task delivers profiles and retrieval. Automatic transcript ingestion, corrections, deletion policy, durable action replay, avatar, and barge-in need their own acceptance criteria.

## Entry gate: close the live conversation evidence gap

- [ ] Read the latest conversation refactor report and run `uv run pytest`.
- [ ] Use a currently hosted test Meet supplied by the user. Run the existing Linux worker with `--audio --converse`, a fresh meeting id, and a short duration.
- [ ] Ask two linked questions: introduce a synthetic project name, then ask for that name. Confirm both replies are audible and the second uses the first turn's context.
- [ ] Stop during model work and confirm no late response, an ended lifecycle, and no remaining meeting container.
- [ ] Record local transcript, graph result, TTS submission, and remote hearing as separate evidence. An unavailable host leaves this gate open; profile unit work can continue, but live acceptance must not be marked passed.

## File map

| File | Responsibility |
| --- | --- |
| `src/fakeyou/persona.py` (new) | Validated profile, identity check, prompt rendering, namespace derivation |
| `src/fakeyou/cli.py` | Load optional persona and compose the existing agent with permitted tools |
| `tests/test_persona.py` (new) | Validation, namespace isolation, prompt data |
| `tests/test_cli.py` | CLI contract and actual composed tool list |
| `tests/test_providers_live.py` | Opt-in isolation check using synthetic facts in disposable groups |
| `README.md`, `docs/project-status.md` | Commands, compatibility notes, measured verification |

## Task 1: profile contract and owner identity

**Produces:** `PersonaProfile`, `load_persona(path: Path, session: SessionConfig) -> PersonaProfile`, and `build_persona(profile: PersonaProfile) -> str`.

- [ ] Add failing tests for unknown fields, empty identity/name, a profile for a different owner/twin, and memory isolation. Use the same profile across two meetings and assert it retains its memory namespace.

```python
def test_owner_memory_namespace_is_stable_and_isolated():
    first = PersonaProfile(owner_id="owner-a", twin_id="twin", display_name="Twin")
    same = PersonaProfile(owner_id="owner-a", twin_id="twin", display_name="Renamed Twin")
    other = PersonaProfile(owner_id="owner-b", twin_id="twin", display_name="Twin")
    assert first.memory_group == same.memory_group
    assert first.memory_group != other.memory_group
```

- [ ] Run `uv run pytest tests/test_persona.py -q` and confirm the missing contract fails.
- [ ] Implement the schema with the existing `SessionId` and `TwinName` validated types. Use Pydantic's `ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)`, not a custom JSON/schema parser.

```python
class PersonaProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    owner_id: SessionId
    twin_id: SessionId
    display_name: TwinName
    role: str = "meeting participant"
    language: str = "id"
    speaking_style: str = "brief and clear"
    goals: tuple[str, ...] = ()
    verified_facts: tuple[str, ...] = ()
    allowed_tools: frozenset[Literal["recall_meeting_context"]] = frozenset()

    @property
    def memory_group(self) -> str:
        identity = json.dumps([self.owner_id, self.twin_id])
        return f"twin_{uuid5(NAMESPACE_URL, identity).hex}"
```

- [ ] `load_persona` uses `PersonaProfile.model_validate_json(path.read_text())`, then rejects an owner/twin mismatch with `ValueError`. The async caller runs this file operation through `asyncio.to_thread` before opening model, Graphiti, or browser resources.
- [ ] `build_persona` combines `DEFAULT_PERSONA` with explicit voice guidance and `profile.model_dump_json()`: answer briefly in the configured language, distinguish profile facts from participants' claims and model inference, and never invent owner authority or speaker identity. Keep task instructions outside the JSON profile data.
- [ ] Run the profile tests, Ruff, and mypy. Review and commit this independently testable contract.

## Task 2: compose the profile and scoped retrieval

**Consumes:** Task 1 profile functions and existing `open_conversation`, `create_memory_tools`, and `create_meeting_agent`.

**Produces:** `join --audio --converse --persona PATH`, with `--memory` exposing only profile-permitted recall tools.

- [ ] Extend CLI tests so `--persona` requires `--converse`, mismatched identity fails before runtime startup, and no write tool reaches the meeting agent.

```python
assert captured_persona == build_persona(profile)
assert captured_group_id == profile.memory_group
assert [tool.name for tool in captured_tools] == ["recall_meeting_context"]
```

- [ ] Run `uv run pytest tests/test_cli.py -q`; confirm the new contracts fail before changing composition.
- [ ] Add `persona_path: str | None = None` to `join_meeting`. Validate the file after `SessionConfig` is constructed and before entering external resources. Pass `profile: PersonaProfile | None = None` into `open_conversation`.
- [ ] In the conversation path, require `--persona` when `--memory` is set. Keep the independent `chat --memory` command compatible with its explicitly configured group. Document this distinction.
- [ ] Inject `build_persona(profile)` into `create_meeting_agent`. For `--memory`, use the profile-derived namespace and filter the native tools before constructing the agent:

```python
available = create_memory_tools(graphiti, group_id=profile.memory_group)
tools = [tool for tool in available if tool.name in profile.allowed_tools]
```

- [ ] Verify a profile without `recall_meeting_context` receives no memory tools. Preserve `--audio` transcription-only behavior and all cancellation/cleanup tests.
- [ ] Run `uv run pytest tests/test_cli.py tests/test_conversation.py tests/test_runtime_audio.py -q`, Ruff, and mypy; review and commit the integration.

## Task 3: verify isolation and document the next boundary

- [ ] Extend the opt-in Graphiti test with two unique owner namespaces and contradictory synthetic facts. Use the existing native client context and exact episode UUID cleanup pattern. Reopen clients before retrieval; neither owner may retrieve the other's fact.
- [ ] Do not send real owner knowledge during provider verification. Reuse the test suite's installed SDK APIs and cleanup only its own episode ids.
- [ ] Run the full offline suite and DeepEval, then the selected live test only with configured credentials. Record each result separately.
- [ ] Update README with a complete synthetic JSON profile and the `--persona` command. Keep actual personal profiles out of version control unless explicitly intended as fixtures.
- [ ] Record the live conversation entry-gate outcome. Do not call Stage 4 complete: ingestion with source ids, event time, correction, retention, and durable write idempotency is the following slice.
- [ ] Review the final diff and commit. Report the actual test evidence and remaining open gates.

## Documentation to consult

- [Pydantic model validation](https://docs.pydantic.dev/latest/concepts/models/#validating-data).
- [LangChain short-term memory](https://docs.langchain.com/oss/python/langchain/short-term-memory).
- [Graphiti graph namespacing](https://help.getzep.com/graphiti/core-concepts/graph-namespacing).
- [Graphiti episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes).
- Installed Graphiti 0.30.2 `Graphiti.search`, `Graphiti.add_episode`, `EpisodicNode`, and the existing project's live cleanup test before changing persistence tests.
