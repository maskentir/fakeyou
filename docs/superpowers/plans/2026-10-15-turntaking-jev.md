# Turn-Taking Generik Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Twin memutuskan bicara/diam/klarifikasi secara generik untuk meeting apapun lewat rule guard plus Jev System One pre-generate.

**Architecture:** Rule guard sinkron tetap memutus kasus jelas dengan nol latency baru; kasus abu-abu memakai keputusan Jev yang di pre-generate saat orang lain bicara. Responder Gemini/LLM existing tetap System Two untuk menghasilkan ucapan.

**Tech Stack:** Python 3.12, httpx2 2.13.1 (existing, pola `src/fakeyou/speech/minimax.py`), Pydantic 2 Settings, pytest, DeepEval offline fixture.

**Spec:** `docs/superpowers/specs/2026-10-15-turntaking-jev-design.md`

## Global Constraints

- Python code Inggris tanpa komentar dan docstring baru; dokumen Bahasa Indonesia.
- Tidak tambah dependency atau lockfile; HTTP Jev via `httpx2` existing.
- Setiap peran memakai key sendiri; tidak ada peminjaman key lintas peran.
- Journal hanya metadata (ID, waktu, jumlah, durasi, status, confidence, reason); tidak ada teks transkrip, jawaban, prompt, recall, atau secret di journal.
- Durasi journal `_ms`; konfigurasi dan laporan `_seconds`.
- Jev endpoint `POST /v1/systemone`, field `model` memilih model, default `jev-latest`; questions paralel Choice plus Noul; output gratis; latensi 70-500 ms.
- Threshold awal `speak_threshold=0.70`, `clarify_threshold=0.50`; wajib di-tune dari corpus sebelum live, bukan 0.5 fixed.
- `window_turns` default 6.
- Unknown adalah hasil sah; unknown tanpa alamat eksplisit tetap silent; tidak ada tebakan nama.
- Semua final direkam sebelum policy; silent tidak menghapus ucapan dari record/ingestion.
- Breaking yang disetujui: `ParticipationContext` tambah field, `_decide` terstruktur, brief v1 ke v2 dengan `roster_hint` opsional dan baca v1 tetap valid.

---

### Task 1: TurnTracker pure

**Files:**
- Create: `src/fakeyou/foundation/turn_tracker.py`
- Test: `tests/test_turn_tracker.py`

**Interfaces:**
- Consumes: `fakeyou.foundation.transcript.TranscriptEvent` (`id`, `text`, `participant_id`)
- Produces: `TurnState(spoken_ids: tuple[str, ...], floor_holder: str | None, cue: Literal["explicit","implicit","none"], cue_target: str | None, my_turn_likely: bool)`, `empty_turn_state() -> TurnState`, `detect_turn_cue(text: str, twin_names: Sequence[str]) -> tuple[cue, target]`, `note_event(state: TurnState, event: TranscriptEvent, twin_names: Sequence[str]) -> TurnState`

- [ ] **Step 1: Write the failing test**

```python
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.foundation.turn_tracker import detect_turn_cue, empty_turn_state, note_event


def _event(id: str, text: str, participant_id: str) -> TranscriptEvent:
    return TranscriptEvent(
        id=id,
        timestamp="2026-10-15T00:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id=participant_id,
        speaker_evidence_id=None,
    )


def test_explicit_cue_with_name_sets_my_turn():
    cue, target = detect_turn_cue("Harun, you're up", ("harun",))
    assert (cue, target) == ("explicit", "harun")


def test_quote_about_twin_is_not_a_cue():
    cue, _ = detect_turn_cue('kata dia "Harun setuju"', ("harun",))
    assert cue == "none"


def test_unknown_ids_never_join_spoken_order():
    state = empty_turn_state()
    state = note_event(state, _event("t1", "halo", "unknown"), ("harun",))
    state = note_event(state, _event("t2", "halo", "unknown"), ("harun",))
    assert state.spoken_ids == ()
    assert state.my_turn_likely is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/test_turn_tracker.py -q`
Expected: FAIL with "No module named 'fakeyou.foundation.turn_tracker'"

- [ ] **Step 3: Write minimal implementation**

```python
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from fakeyou.foundation.transcript import TranscriptEvent

TurnCue = Literal["explicit", "implicit", "none"]


@dataclass(frozen=True, slots=True)
class TurnState:
    spoken_ids: tuple[str, ...] = ()
    floor_holder: str | None = None
    cue: TurnCue = "none"
    cue_target: str | None = None
    my_turn_likely: bool = False


def empty_turn_state() -> TurnState:
    return TurnState()


def detect_turn_cue(text: str, twin_names: Sequence[str]) -> tuple[TurnCue, str | None]:
    folded = text.strip().casefold()
    for raw in twin_names:
        name = raw.strip().casefold()
        if not name:
            continue
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", folded) is None:
            continue
        if folded.startswith("next") or "you're up" in folded or folded.startswith("silakan"):
            return ("explicit", name)
        if re.search(rf"(gimana|bagaimana)\s+{re.escape(name)}", folded) is not None:
            return ("explicit", name)
        if folded.startswith(name):
            return ("explicit", name)
    if folded == "next" or "you're up" in folded or folded.startswith("silakan"):
        return ("implicit", None)
    return ("none", None)


def note_event(state: TurnState, event: TranscriptEvent, twin_names: Sequence[str]) -> TurnState:
    spoken = state.spoken_ids
    if event.participant_id != "unknown" and event.participant_id not in spoken:
        spoken = (*spoken, event.participant_id)
    cue, target = detect_turn_cue(event.text, twin_names)
    mine = cue == "explicit" or (cue == "implicit" and "harun" not in spoken)
    return TurnState(
        spoken_ids=spoken,
        floor_holder=event.participant_id if event.participant_id != "unknown" else state.floor_holder,
        cue=cue,
        cue_target=target,
        my_turn_likely=mine,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest tests/test_turn_tracker.py tests/test_participation.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/fakeyou/foundation/turn_tracker.py tests/test_turn_tracker.py
git commit -m "feat: add pure turn tracker with explicit cue detection"
```

### Task 2: ParticipationContext turn fields plus rule priority

**Files:**
- Modify: `src/fakeyou/foundation/participation.py`
- Test: `tests/test_participation.py`

**Interfaces:**
- Consumes: `TurnState` from Task 1
- Produces: `ParticipationContext(addressed, followup, same_participant, ambiguous, others_speaking, activation_valid, bare_ack=False, interjection=False, awaiting_reply=False, turn_cue: str = "none", turn_mine: bool = False)`, `choose_participation` unchanged priority plus `turn_mine` only feeds `reason` suffix, never overrides silent for occupied floor

- [ ] **Step 1: Write the failing test**

```python
def test_turn_mine_does_not_override_occupied_floor():
    context = ParticipationContext(
        addressed=False,
        followup=True,
        same_participant=True,
        ambiguous=False,
        others_speaking=True,
        activation_valid=True,
        turn_cue="explicit",
        turn_mine=True,
    )
    assert choose_participation(context).action == "silent"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/test_participation.py -q`
Expected: FAIL with "unexpected keyword argument 'turn_cue'"

- [ ] **Step 3: Write minimal implementation**

```python
@dataclass(frozen=True, slots=True)
class ParticipationContext:
    addressed: bool
    followup: bool
    same_participant: bool
    ambiguous: bool
    others_speaking: bool
    activation_valid: bool
    bare_ack: bool = False
    interjection: bool = False
    awaiting_reply: bool = False
    turn_cue: str = "none"
    turn_mine: bool = False
```

Keep `choose_participation` body exactly as-is; defaults preserve all 282 existing assertions.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest tests/test_participation.py tests/test_turn_tracker.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/fakeyou/foundation/participation.py tests/test_participation.py
git commit -m "feat: extend participation context with turn fields"
```

### Task 3: Participation decider settings

**Files:**
- Modify: `src/fakeyou/config/settings.py`
- Test: `tests/test_cli_join_options.py` (assert env parsing) or new `tests/test_participation_settings.py`

**Interfaces:**
- Consumes: `Section`, `PositiveSeconds`, `SecretStr` patterns from settings module
- Produces: `ParticipationDeciderSettings(model: str = "jev-latest", base_url: OptionalURL = None, api_key: SecretStr | None = None, timeout_seconds: PositiveSeconds = 2.0, window_turns: int = 6, speak_threshold: float = 0.70, clarify_threshold: float = 0.50, pre_generate_enabled: bool = True)`, `Configuration.participation_decider` (env `FAKEYOU_PARTICIPATION_DECIDER__*`)

- [ ] **Step 1: Write the failing test**

```python
def test_participation_decider_defaults_are_conservative(settings):
    decider = settings.participation_decider
    assert decider.model == "jev-latest"
    assert decider.window_turns == 6
    assert decider.speak_threshold == 0.70
    assert decider.clarify_threshold == 0.50
    assert decider.pre_generate_enabled is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/test_participation_settings.py -q`
Expected: FAIL with "no such file or no attribute participation_decider"

- [ ] **Step 3: Write minimal implementation**

```python
class ParticipationDeciderSettings(Section):
    model: str = "jev-latest"
    base_url: OptionalURL = None
    api_key: SecretStr | None = None
    timeout_seconds: PositiveSeconds = 2.0
    window_turns: int = Field(default=6, gt=0, le=20)
    speak_threshold: float = Field(default=0.70, gt=0, lt=1)
    clarify_threshold: float = Field(default=0.50, gt=0, lt=1)
    pre_generate_enabled: bool = True
```

Add field on `Configuration`:

```python
participation_decider: ParticipationDeciderSettings = Field(default_factory=ParticipationDeciderSettings)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest tests/test_participation_settings.py tests/test_cli_join_options.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/fakeyou/config/settings.py tests/test_participation_settings.py
git commit -m "feat: add participation decider settings with conservative thresholds"
```

### Task 4: JevDecider adapter with injected transport

**Files:**
- Create: `src/fakeyou/conversation/participation_decider.py`
- Test: `tests/test_participation_decider.py`

**Interfaces:**
- Consumes: `TurnState`, settings values as plain args (no Settings import in adapter)
- Produces: `JevDecision(action: Literal["speak","silent","clarify"], confidence: float, addressed_p: float, conversing_p: float, clarify_p: float)`, `build_jev_payload(state_text: str, model: str) -> dict`, `parse_jev_response(body: dict) -> JevDecision`, `JevDecider(api_key, model, base_url, timeout_seconds, transport=None)` with `async decide(state_text: str) -> JevDecision`; transport signature `Callable[[dict], Awaitable[dict]]`; real transport posts `{"model","state","questions":{"decision":{"type":"choice","options":["speak","silent","clarify"]},"addressed_to_me":{"type":"noul","instructions":"..."},"others_conversing":{"type":"noul","instructions":"..."},"needs_clarify":{"type":"noul","instructions":"..."}}}` to `{base_url}/v1/systemone` via `httpx2.AsyncClient`; any transport error returns `JevDecision("silent", 0.0, 0.0, 0.0, 0.0)`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from fakeyou.conversation.participation_decider import JevDecider, build_jev_payload, parse_jev_response


async def _fake_transport(payload: dict) -> dict:
    assert payload["model"] == "jev-latest"
    assert set(payload["questions"]) == {"decision", "addressed_to_me", "others_conversing", "needs_clarify"}
    return {
        "decision": {"choice": "speak", "confidence": 0.82, "probs": {"speak": 0.82, "silent": 0.12, "clarify": 0.06}},
        "addressed_to_me": {"probability": 0.9},
        "others_conversing": {"probability": 0.05},
        "needs_clarify": {"probability": 0.1},
    }


def test_payload_contains_parallel_questions():
    payload = build_jev_payload("Harun, you're up", "jev-latest")
    assert payload["questions"]["decision"]["type"] == "choice"


def test_parse_maps_choice_and_confidence():
    body = {
        "decision": {"choice": "clarify", "confidence": 0.61, "probs": {}},
        "addressed_to_me": {"probability": 0.4},
        "others_conversing": {"probability": 0.4},
        "needs_clarify": {"probability": 0.66},
    }
    decision = parse_jev_response(body)
    assert (decision.action, decision.confidence) == ("clarify", 0.61)


@pytest.mark.asyncio
async def test_decider_uses_injected_transport():
    decider = JevDecider(api_key="k", model="jev-latest", base_url="https://x", timeout_seconds=2.0, transport=_fake_transport)
    decision = await decider.decide("state")
    assert decision.action == "speak"
    await decider.aclose()


@pytest.mark.asyncio
async def test_transport_error_falls_back_silent():
    async def _boom(payload: dict) -> dict:
        raise TimeoutError("jev down")
    decider = JevDecider(api_key="k", model="jev-latest", base_url="https://x", timeout_seconds=2.0, transport=_boom)
    decision = await decider.decide("state")
    assert (decision.action, decision.confidence) == ("silent", 0.0)
    await decider.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/test_participation_decider.py -q`
Expected: FAIL with "No module named 'fakeyou.conversation.participation_decider'"

- [ ] **Step 3: Write minimal implementation**

```python
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx2

JevAction = Literal["speak", "silent", "clarify"]


@dataclass(frozen=True, slots=True)
class JevDecision:
    action: JevAction
    confidence: float
    addressed_p: float
    conversing_p: float
    clarify_p: float


def build_jev_payload(state_text: str, model: str) -> dict[str, Any]:
    return {
        "model": model,
        "state": state_text,
        "questions": {
            "decision": {"type": "choice", "options": ["speak", "silent", "clarify"]},
            "addressed_to_me": {"type": "noul", "instructions": "The last utterance is addressed to the twin"},
            "others_conversing": {"type": "noul", "instructions": "This is part of a conversation between two other people"},
            "needs_clarify": {"type": "noul", "instructions": "The referent is ambiguous and a neutral clarification is warranted"},
        },
    }


def parse_jev_response(body: dict[str, Any]) -> JevDecision:
    decision = body.get("decision") if isinstance(body, dict) else None
    action = decision.get("choice") if isinstance(decision, dict) else None
    if action not in ("speak", "silent", "clarify"):
        return JevDecision("silent", 0.0, 0.0, 0.0, 0.0)
    confidence = decision.get("confidence", 0.0)
    def _prob(key: str) -> float:
        node = body.get(key)
        value = node.get("probability") if isinstance(node, dict) else None
        return value if isinstance(value, (int, float)) else 0.0
    return JevDecision(action, float(confidence), _prob("addressed_to_me"), _prob("others_conversing"), _prob("needs_clarify"))


class JevDecider:
    def __init__(self, *, api_key: str, model: str, base_url: str, timeout_seconds: float, transport: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None) -> None:
        self._payload_model = model
        self._url = base_url.rstrip("/") + "/v1/systemone"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._timeout = timeout_seconds
        self._transport = transport
        self._client = httpx2.AsyncClient(timeout=timeout_seconds) if transport is None else None

    async def decide(self, state_text: str) -> JevDecision:
        payload = build_jev_payload(state_text, self._payload_model)
        try:
            if self._transport is not None:
                body = await self._transport(payload)
            else:
                assert self._client is not None
                response = await self._client.post(self._url, headers=self._headers, json=payload)
                response.raise_for_status()
                body = response.json()
        except Exception:
            return JevDecision("silent", 0.0, 0.0, 0.0, 0.0)
        try:
            return parse_jev_response(body)
        except Exception:
            return JevDecision("silent", 0.0, 0.0, 0.0, 0.0)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest tests/test_participation_decider.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/fakeyou/conversation/participation_decider.py tests/test_participation_decider.py
git commit -m "feat: add Jev decider adapter with silent fallback"
```

### Task 5: ConversationLoop pre-generate integration

**Files:**
- Modify: `src/fakeyou/conversation/conversation.py:1021-1068`
- Test: `tests/test_conversation_core_turns.py`

**Interfaces:**
- Consumes: `TurnState` plus `note_event`, `JevDecision`, `choose_participation`
- Produces: `_decide` returns structured reason among `rule_addressed`, `rule_floor_occupied`, `jev_speak`, `jev_silent`, `jev_clarify`, `jev_stale_silent`; pregen cache keyed by `tuple(last N event ids) + turn.cue + turn.my_turn_likely`; stale or missing pregen yields silent without blocking; journal `policy_decided` gains `source=rule|jev`, `confidence`, `turn_cue`

- [ ] **Step 1: Write the failing test**

```python
from fakeyou.conversation.conversation import pregen_cache_key


def test_pregen_key_changes_when_turn_cue_changes():
    key_a = pregen_cache_key(("t1", "t2"), "none", False)
    key_b = pregen_cache_key(("t1", "t2"), "explicit", True)
    assert key_a != key_b


def test_stale_key_is_rejected():
    assert pregen_cache_key(("old-id",), "explicit", True) != pregen_cache_key(("new-id",), "explicit", True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/test_conversation_core_turns.py -q -k pregen_key`
Expected: FAIL with "cannot import name 'pregen_cache_key'"

- [ ] **Step 3: Write minimal implementation**

```python
def pregen_cache_key(event_ids: tuple[str, ...], cue: str, mine: bool) -> tuple:
    return (*event_ids[-6:], cue, mine)


JEV_TO_PARTICIPATION = {"speak": "reply", "silent": "silent", "clarify": "clarify"}
```

In `_decide`: run `choose_participation` first; if action silent with `floor_occupied` record reason `rule_floor_occupied` and return False; if addressed record `rule_addressed` and return True; else look up `self._jev_pregen` by `pregen_cache_key(tuple(e.id for e in self._events), self._turn.cue, self._turn.my_turn_likely)`; if hit and confidence meets `speak_threshold` or `clarify_threshold`, map via `JEV_TO_PARTICIPATION` and record `jev_speak`, `jev_silent`, or `jev_clarify`; else record `jev_stale_silent` and return False. Pregen refresh triggered from `handle_interim` path only when floor is free, never blocking `handle_transcript`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest tests/test_conversation_core_turns.py tests/test_participation.py tests/test_participation_decider.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/fakeyou/conversation/conversation.py tests/test_conversation_core_turns.py
git commit -m "feat: integrate jev pregen with rule guard and stale fallback"
```

### Task 6: Brief v2 roster_hint with v1 read compat

**Files:**
- Modify: `src/fakeyou/persistence/brief.py`, `examples/meeting-brief.example.json`
- Test: `tests/test_brief_join_snapshot.py`

**Interfaces:**
- Consumes: `MeetingBrief` v1 JSON
- Produces: `MeetingBrief(schema_version: Literal["meeting_brief/v1","meeting_brief/v2"], roster_hint: tuple[BriefText, ...] = ())`; `load_meeting_brief` accepts v1 and v2; `brief_digest` covers new field; example shows `"schema_version": "meeting_brief/v2"` plus `"roster_hint": ["Ayu", "Harun"]`

- [ ] **Step 1: Write the failing test**

```python
def test_v1_brief_without_roster_hint_still_loads(tmp_path):
    payload = {
        "schema_version": "meeting_brief/v1",
        "owner_id": "o",
        "twin_id": "t",
        "meeting_id": "m",
        "agenda": ["Standup"],
    }
    path = tmp_path / "brief.json"
    path.write_text(__import__("json").dumps(payload))
    brief = load_meeting_brief(path, owner_id="o", twin_id="t", meeting_id="m")
    assert brief.roster_hint == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest tests/test_brief_join_snapshot.py -q -k v1_brief_without_roster`
Expected: FAIL with "roster_hint" or "extra" validation error

- [ ] **Step 3: Write minimal implementation**

```python
class MeetingBrief(BaseModel):
    schema_version: Literal["meeting_brief/v1", "meeting_brief/v2"]
    roster_hint: Annotated[tuple[BriefText, ...], Field(max_length=20)] = ()
```

Keep `extra="forbid"`, frozen, validators unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest tests/test_brief_join_snapshot.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/fakeyou/persistence/brief.py examples/meeting-brief.example.json tests/test_brief_join_snapshot.py
git commit -m "feat: add brief v2 roster hint with v1 read compat"
```

### Task 7: Report plus labeled corpus eval harness

**Files:**
- Modify: `src/fakeyou/observability/report.py`
- Create: `evals/fixtures/participation-corpus.json`, `evals/test_participation_decider_eval.py`

**Interfaces:**
- Consumes: journal `policy_decided` rows with `source`, `confidence`, `turn_cue`
- Produces: report fields `rule_vs_jev` counts, `stale_pregen` count, `false_speak_rate`, `false_silent_rate`; corpus fixture list of `{id, text, participant_id, label: speak|silent|clarify, reason}`; eval computes rates without network by default via fake transport, live Jev behind `-m live`

- [ ] **Step 1: Write the failing test**

```python
def test_corpus_harness_measures_false_speak_and_silent():
    from evals.test_participation_decider_eval import score_corpus
    corpus = [
        {"id": "c1", "text": "Harun, you're up", "participant_id": "a", "label": "speak", "reason": "explicit cue"},
        {"id": "c2", "text": "kita lanjutin PR kemarin ya", "participant_id": "a", "label": "silent", "reason": "others conversing"},
    ]
    async def _speak(payload):
        return {"decision": {"choice": "speak", "confidence": 0.9, "probs": {}}, "addressed_to_me": {"probability": 0.9}, "others_conversing": {"probability": 0.0}, "needs_clarify": {"probability": 0.0}}
    result = score_corpus(corpus, transport=_speak)
    assert result["false_speak_rate"] == 0.5
    assert result["false_silent_rate"] == 0.0


def test_report_summary_counts_rule_jev_and_stale():
    from evals.test_participation_decider_eval import summarize_policy
    rows = [
        {"event": "policy_decided", "source": "rule", "reason": "rule_addressed"},
        {"event": "policy_decided", "source": "jev", "reason": "jev_speak"},
        {"event": "policy_decided", "source": "jev", "reason": "jev_stale_silent"},
    ]
    assert summarize_policy(rows) == {"rule": 1, "jev": 2, "stale_pregen": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync pytest evals/test_participation_decider_eval.py -q`
Expected: FAIL with "No module" or "no function score_corpus"

- [ ] **Step 3: Write minimal implementation**

```python
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


def summarize_policy(rows: list[dict[str, Any]]) -> dict[str, int]:
    summary = {"rule": 0, "jev": 0, "stale_pregen": 0}
    for row in rows:
        if row.get("event") != "policy_decided":
            continue
        source = row.get("source", "rule")
        if source in summary:
            summary[source] += 1
        if row.get("reason") == "jev_stale_silent":
            summary["stale_pregen"] += 1
    return summary


def score_corpus(corpus: list[dict[str, Any]], transport: Callable[[dict], Awaitable[dict]]) -> dict[str, float]:
    from fakeyou.conversation.participation_decider import JevDecider
    async def _run() -> dict[str, float]:
        decider = JevDecider(api_key="test", model="jev-latest", base_url="https://test", timeout_seconds=1.0, transport=transport)
        false_speak = 0
        false_silent = 0
        speak_den = 0
        silent_den = 0
        for row in corpus:
            decision = await decider.decide(row["text"])
            predicted = "speak" if decision.action == "speak" else "silent"
            if row["label"] == "speak":
                speak_den += 1
                if predicted != "speak":
                    false_silent += 1
            else:
                silent_den += 1
                if predicted == "speak":
                    false_speak += 1
        await decider.aclose()
        return {
            "false_speak_rate": (false_speak / silent_den) if silent_den else 0.0,
            "false_silent_rate": (false_silent / speak_den) if speak_den else 0.0,
        }
    return asyncio.run(_run())
```

Seed `evals/fixtures/participation-corpus.json` with 12 rows covering explicit cue, quote-not-cue, others-conversing, followup same versus different speaker, overlap unknown, activation expired.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --no-sync pytest evals/test_participation_decider_eval.py tests/test_turn_tracker.py tests/test_participation.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/fakeyou/observability/report.py evals/fixtures/participation-corpus.json evals/test_participation_decider_eval.py
git commit -m "test: add labeled participation corpus with false speak silent rates"
```
