# Rencana implementasi turn-taking generik

> **Untuk pekerja agen:** SUB-SKILL WAJIB: pakai superpowers:subagent-driven-development (disarankan) atau superpowers:executing-plans untuk mengerjakan rencana ini tugas per tugas. Langkah memakai sintaks checkbox (`- [ ]`) untuk pelacakan.

**Tujuan:** Twin memutuskan bicara/diam/klarifikasi secara generik untuk meeting apapun lewat rule guard plus Jev System One pre-generate.

**Arsitektur:** Rule guard sinkron tetap memutus kasus jelas tanpa latensi baru; kasus abu-abu memakai keputusan Jev hasil pre-generate saat peserta lain bicara. Responder Gemini/LLM existing tetap System Two untuk menghasilkan ucapan.

**Stack teknologi:** Python 3.12, httpx2 2.13.1 (existing, pola `src/fakeyou/speech/minimax.py`), Pydantic 2 Settings, pytest, fixture offline DeepEval.

**Spec:** `turntaking-jev-design.md` (satu folder dengan berkas ini; rencana ini berargumen dari spec tersebut, pelaksana membaca keduanya)

## Batasan global

- Kode Python berbahasa Inggris tanpa komentar dan docstring baru; dokumen berbahasa Indonesia.
- Tanpa tambah dependency atau lockfile; HTTP Jev via `httpx2` existing.
- Setiap peran memakai key sendiri; tanpa peminjaman key lintas peran.
- Journal hanya metadata (ID, waktu, jumlah, durasi, status, confidence, reason); tanpa teks transkrip, jawaban, prompt, recall, atau secret di journal.
- Durasi journal `_ms`; konfigurasi dan laporan `_seconds`.
- Jev endpoint `POST /v1/systemone`, field `model` memilih model, default `jev-latest`; questions paralel Choice plus Noul; output gratis; latensi 70-500 ms.
- Threshold awal `speak_threshold=0.70`, `clarify_threshold=0.50`; wajib di-tune dari corpus sebelum live, bukan 0.5 fixed.
- `window_turns` default 6.
- Unknown adalah hasil sah; unknown tanpa alamat eksplisit tetap silent; tanpa tebakan nama.
- Semua final direkam sebelum policy; silent tidak menghapus ucapan dari record/ingestion.
- Breaking disetujui: `ParticipationContext` tambah field, `_decide` terstruktur, brief v1 ke v2 plus `roster_hint` opsional dan baca v1 tetap valid.
- Kosakata Indonesia (cue giliran, sapaan) tinggal di berkas data JSON, bukan di sumber Python, mengikuti pola `ack_vocabulary.json`; aturan `test_python_files_contain_no_indonesian` menolak literal Indonesia di `.py`.

---

### Task 1: TurnTracker pure

**Berkas:**
- Buat: `src/fakeyou/foundation/turn_tracker.py`, `src/fakeyou/turn_cues.json`
- Tes: `tests/test_turn_tracker.py`

**Antarmuka:**
- Memakai: `fakeyou.foundation.transcript.TranscriptEvent` (`id`, `text`, `participant_id`)
- Menghasilkan: `TurnState(spoken_ids, floor_holder, cue: explicit/implicit/none, cue_target, my_turn_likely)`, `empty_turn_state() -> TurnState`, `detect_turn_cue(text, twin_names) -> tuple[cue, target]`, `note_event(state, event, twin_names) -> TurnState`; kosakata cue dibaca dari `turn_cues.json` (`named_cue_prefixes`, `named_cue_markers`, `question_openers`)

- [ ] **Langkah 1: Tulis tes merah**

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
    cue, _ = detect_turn_cue('she said "Harun agrees"', ("harun",))
    assert cue == "none"


def test_unknown_ids_never_join_spoken_order():
    state = empty_turn_state()
    state = note_event(state, _event("t1", "hello", "unknown"), ("harun",))
    state = note_event(state, _event("t2", "hello", "unknown"), ("harun",))
    assert state.spoken_ids == ()
    assert state.my_turn_likely is False
```

- [ ] **Langkah 2: Jalankan tes untuk memastikan merah**

Run: `uv run --no-sync pytest tests/test_turn_tracker.py -q`
Hasil: FAIL karena modul `fakeyou.foundation.turn_tracker` belum ada

- [ ] **Langkah 3: Tulis implementasi minimal**

```python
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from fakeyou.foundation.resources import read_package_text
from fakeyou.foundation.transcript import TranscriptEvent

TurnCue = Literal["explicit", "implicit", "none"]

_TURN_CUES: dict[str, Any] = json.loads(read_package_text("turn_cues.json"))


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
    openers = "|".join(re.escape(opener) for opener in _TURN_CUES["question_openers"])
    for raw in twin_names:
        name = raw.strip().casefold()
        if not name:
            continue
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", folded) is None:
            continue
        if _named_cue(folded):
            return ("explicit", name)
        if re.search(rf"({openers})\s+{re.escape(name)}", folded) is not None:
            return ("explicit", name)
        if folded.startswith(name):
            return ("explicit", name)
    if folded == "next" or _named_cue(folded):
        return ("implicit", None)
    return ("none", None)
```

Penyebutan nama di kutipan atau kalimat tentang twin bukan cue; dua ID `unknown` tidak pernah dianggap penutur sama.

- [ ] **Langkah 4: Jalankan tes untuk memastikan hijau**

Run: `uv run --no-sync pytest tests/test_turn_tracker.py tests/test_participation.py -q`
Hasil: PASS

- [ ] **Langkah 5: Commit**

```bash
git add src/fakeyou/foundation/turn_tracker.py src/fakeyou/turn_cues.json tests/test_turn_tracker.py
git commit -m "feat: add pure turn tracker with explicit cue detection"
```

### Task 2: Field giliran di ParticipationContext plus prioritas rule

**Berkas:**
- Ubah: `src/fakeyou/foundation/participation.py`
- Tes: `tests/test_participation.py`

**Antarmuka:**
- Memakai: `TurnState` dari Task 1
- Menghasilkan: `ParticipationContext(addressed, followup, same_participant, ambiguous, others_speaking, activation_valid, bare_ack=False, interjection=False, awaiting_reply=False, turn_cue: str = "none", turn_mine: bool = False)`; prioritas `choose_participation` tidak berubah; `turn_mine` tidak pernah mengalahkan silent saat floor occupied

- [ ] **Langkah 1: Tulis tes merah**

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

- [ ] **Langkah 2: Jalankan tes untuk memastikan merah**

Run: `uv run --no-sync pytest tests/test_participation.py -q`
Hasil: FAIL karena konstruktor belum mengenal `turn_cue`

- [ ] **Langkah 3: Tulis implementasi minimal**

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

Biarkan badan `choose_participation` apa adanya; default menjaga seluruh asersi existing tetap hijau.

- [ ] **Langkah 4: Jalankan tes untuk memastikan hijau**

Run: `uv run --no-sync pytest tests/test_participation.py tests/test_turn_tracker.py -q`
Hasil: PASS

- [ ] **Langkah 5: Commit**

```bash
git add src/fakeyou/foundation/participation.py tests/test_participation.py
git commit -m "feat: extend participation context with turn fields"
```

### Task 3: Settings decider partisipasi

**Berkas:**
- Ubah: `src/fakeyou/config/settings.py`, `docs/reference/environment.md` (hasil regenerate katalog), `.env.example`
- Tes: `tests/test_participation_settings.py` baru (memakai `Settings()` langsung mengikuti pola `tests/test_settings.py`)

**Antarmuka:**
- Memakai: pola `Section`, `PositiveSeconds`, `SecretStr` dari modul settings
- Menghasilkan: `ParticipationDeciderSettings(model: str = "jev-latest", base_url: OptionalURL = None, api_key: SecretStr | None = None, timeout_seconds: PositiveSeconds = 2.0, window_turns: int = 6, speak_threshold: float = 0.70, clarify_threshold: float = 0.50, pre_generate_enabled: bool = True)`; `Configuration.participation_decider` (env `FAKEYOU_PARTICIPATION_DECIDER__*`)

- [ ] **Langkah 1: Tulis tes merah**

```python
def test_participation_decider_defaults_are_conservative() -> None:
    decider = Settings().participation_decider
    assert decider.model == "jev-latest"
    assert decider.window_turns == 6
    assert decider.speak_threshold == 0.70
    assert decider.clarify_threshold == 0.50
    assert decider.pre_generate_enabled is True
```

- [ ] **Langkah 2: Jalankan tes untuk memastikan merah**

Run: `uv run --no-sync pytest tests/test_participation_settings.py -q`
Hasil: FAIL karena atribut `participation_decider` belum ada

- [ ] **Langkah 3: Tulis implementasi minimal**

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

Tambah field di `Configuration`:

```python
participation_decider: ParticipationDeciderSettings = Field(
    default_factory=ParticipationDeciderSettings
)
```

Regenerate halaman katalog (`uv run --no-sync python -m fakeyou.config.env_catalog`) dan dokumentasikan delapan key baru di `.env.example`; key secret tetap dikomentari.

- [ ] **Langkah 4: Jalankan tes untuk memastikan hijau**

Run: `uv run --no-sync pytest tests/test_participation_settings.py tests/test_cli_join_options.py tests/test_settings.py tests/test_settings_explicit.py tests/test_env_catalog.py -q`
Hasil: PASS

- [ ] **Langkah 5: Commit**

```bash
git add src/fakeyou/config/settings.py tests/test_participation_settings.py docs/reference/environment.md .env.example
git commit -m "feat: add participation decider settings with conservative thresholds"
```

### Task 4: Adapter JevDecider plus transport injeksi

**Berkas:**
- Buat: `src/fakeyou/conversation/participation_decider.py`
- Tes: `tests/test_participation_decider.py`

**Antarmuka:**
- Memakai: nilai settings sebagai argumen polos (tanpa import Settings di adapter)
- Menghasilkan: `JevDecision(action: speak/silent/clarify, confidence, addressed_p, conversing_p, clarify_p)`; `build_jev_payload(state_text, model)`; `parse_jev_response(body)`; `JevDecider(api_key, model, base_url, timeout_seconds, transport=None)` plus `async decide(state_text)`; signature transport `Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]`; transport nyata POST `{"model","state","questions"}` ke `{base_url}/v1/systemone` via `httpx2.AsyncClient`; respons tak valid atau error transport menghasilkan silent confidence 0.0; tangkap exception spesifik (`httpx2.HTTPError`, `TimeoutError`, `ValueError` untuk transport; `ValueError`, `TypeError`, `AttributeError` untuk parse) karena aturan BLE001 menolak `except Exception` polos

Satu request Jev memuat questions paralel: `decision` (choice speak/silent/clarify) plus tiga noul (`addressed_to_me`, `others_conversing`, `needs_clarify`).

- [ ] **Langkah 1: Tulis tes merah**

```python
async def _fake_transport(payload: dict) -> dict:
    assert payload["model"] == "jev-latest"
    assert set(payload["questions"]) == {
        "decision",
        "addressed_to_me",
        "others_conversing",
        "needs_clarify",
    }
    return {
        "decision": {
            "choice": "speak",
            "confidence": 0.82,
            "probs": {"speak": 0.82, "silent": 0.12, "clarify": 0.06},
        },
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


async def test_decider_uses_injected_transport():
    decider = JevDecider(
        api_key="k",
        model="jev-latest",
        base_url="https://x",
        timeout_seconds=2.0,
        transport=_fake_transport,
    )
    decision = await decider.decide("state")
    assert decision.action == "speak"
    await decider.aclose()


async def test_transport_error_falls_back_silent():
    async def _boom(_payload: dict) -> dict:
        raise TimeoutError("jev down")

    decider = JevDecider(
        api_key="k",
        model="jev-latest",
        base_url="https://x",
        timeout_seconds=2.0,
        transport=_boom,
    )
    decision = await decider.decide("state")
    assert (decision.action, decision.confidence) == ("silent", 0.0)
    await decider.aclose()
```

Mode `asyncio_mode = "auto"` repo menjalankan tes async tanpa marker; nama argumen tak terpakai wajib diawali underscore (aturan ARG001).

- [ ] **Langkah 2: Jalankan tes untuk memastikan merah**

Run: `uv run --no-sync pytest tests/test_participation_decider.py -q`
Hasil: FAIL karena modul adapter belum ada

- [ ] **Langkah 3: Tulis implementasi minimal**

Lihat `src/fakeyou/conversation/participation_decider.py` hasil akhir: builder payload, parser defensif, dan `JevDecider` plus `aclose`; konstruktor argumen keyword-only; URL `{base_url}/v1/systemone` plus header Bearer.

- [ ] **Langkah 4: Jalankan tes untuk memastikan hijau**

Run: `uv run --no-sync pytest tests/test_participation_decider.py -q`
Hasil: PASS; `mypy` dan `ruff check`/`ruff format --check` bersih untuk berkas baru

- [ ] **Langkah 5: Commit**

```bash
git add src/fakeyou/conversation/participation_decider.py tests/test_participation_decider.py
git commit -m "feat: add Jev decider adapter with silent fallback"
```

### Task 5: Integrasi pre-generate di ConversationLoop

**Berkas:**
- Ubah: `src/fakeyou/conversation/conversation.py` (sekitar `_decide`)
- Tes: `tests/test_conversation_core_turns.py`

**Antarmuka:**
- Memakai: `TurnState` plus `note_event`, `JevDecision`, `choose_participation`
- Menghasilkan: `pregen_cache_key(event_ids, cue, *, mine: bool)` (argumen bool keyword-only karena aturan FBT001); pemetaan `JEV_TO_PARTICIPATION = {"speak": "reply", "silent": "silent", "clarify": "clarify"}`; `_decide` tetap bool; jalur rule mencatat reason existing plus `decision_source="rule"`, `confidence=1.0`, `turn_cue`; kasus abu-abu (rule silent `not_addressed` saat cue giliran muncul) memakai cache pregen: hit plus confidence memenuhi threshold menghasilkan `jev_speak`/`jev_silent`/`jev_clarify`, sedangkan miss atau confidence rendah menghasilkan `jev_stale_silent` tanpa blocking; `store_jev_pregen(decision)` untuk produsen pregen mendatang; refresh pregen live via `handle_interim` menunggu wiring assembly (tindak lanjut di luar milestone ini)

Kolom journal bernama `decision_source` (bukan `source`) karena `source` sudah dipakai journal untuk asal komponen; tabrakan nama terbukti lewat 15 tes merah sebelum penggantian nama.

- [ ] **Langkah 1: Tulis tes merah**

```python
from fakeyou.conversation.conversation import pregen_cache_key


def test_pregen_key_changes_when_turn_cue_changes():
    key_a = pregen_cache_key(("t1", "t2"), "none", mine=False)
    key_b = pregen_cache_key(("t1", "t2"), "explicit", mine=True)
    assert key_a != key_b


def test_stale_key_is_rejected():
    assert pregen_cache_key(("old-id",), "explicit", mine=True) != pregen_cache_key(
        ("new-id",), "explicit", mine=True
    )
```

- [ ] **Langkah 2: Jalankan tes untuk memastikan merah**

Run: `uv run --no-sync pytest tests/test_conversation_core_turns.py -q -k pregen_key`
Hasil: FAIL karena nama belum dapat diimport

- [ ] **Langkah 3: Tulis implementasi minimal**

Tambah helper plus pemetaan di level modul; tambah state `self._turn`, `self._jev_pregen`, dan threshold di `__init__`; perbarui `self._turn` di `handle_transcript` tepat setelah event direkam (sebelum `_decide` berjalan); teruskan `turn_cue`/`turn_mine` ke `ParticipationContext`; cabang abu-abu memanggil `_decide_jev(event)`.

- [ ] **Langkah 4: Jalankan tes untuk memastikan hijau**

Run: `uv run --no-sync pytest tests/test_conversation_core_turns.py tests/test_conversation_policy.py tests/test_participation.py tests/test_turn_tracker.py tests/test_participation_decider.py -q`
Hasil: PASS (205 tes); `mypy` bersih

- [ ] **Langkah 5: Commit**

```bash
git add src/fakeyou/conversation/conversation.py tests/test_conversation_core_turns.py
git commit -m "feat: integrate jev pregen with rule guard and stale fallback"
```

### Task 6: Brief v2 roster_hint plus baca v1 tetap valid

**Berkas:**
- Ubah: `src/fakeyou/persistence/brief.py`, `examples/meeting-brief.example.json`
- Tes: `tests/test_brief_join_snapshot.py`, `tests/test_brief.py` (digest pin dan daftar nilai invalid)

**Antarmuka:**
- Memakai: JSON brief v1
- Menghasilkan: `MeetingBrief(schema_version: v1/v2, roster_hint: tuple[BriefText, ...] = ())`; `load_meeting_brief` menerima v1 dan v2; `brief_digest` mencakup field baru; contoh memakai `"schema_version": "meeting_brief/v2"` plus `"roster_hint": ["Ayu", "Harun"]`; digest pin contoh diperbarui; entri `v2` keluar dari daftar nilai invalid (diganti `v3`)

- [ ] **Langkah 1: Tulis tes merah**

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
    path.write_text(json.dumps(payload))
    brief = load_meeting_brief(path, owner_id="o", twin_id="t", meeting_id="m")
    assert brief.roster_hint == ()
```

- [ ] **Langkah 2: Jalankan tes untuk memastikan merah**

Run: `uv run --no-sync pytest tests/test_brief_join_snapshot.py -q -k v1_brief_without_roster`
Hasil: FAIL karena field `roster_hint` belum dikenal

- [ ] **Langkah 3: Tulis implementasi minimal**

```python
class MeetingBrief(BaseModel):
    schema_version: Literal["meeting_brief/v1", "meeting_brief/v2"]
    roster_hint: Annotated[tuple[BriefText, ...], Field(max_length=20)] = ()
```

Biarkan `extra="forbid"`, frozen, dan validator tidak berubah.

- [ ] **Langkah 4: Jalankan tes untuk memastikan hijau**

Run: `uv run --no-sync pytest tests/test_brief.py tests/test_brief_join_snapshot.py tests/test_brief_join_isolation.py tests/test_brief_join_refusals.py -q`
Hasil: PASS

- [ ] **Langkah 5: Commit**

```bash
git add src/fakeyou/persistence/brief.py examples/meeting-brief.example.json tests/test_brief_join_snapshot.py tests/test_brief.py
git commit -m "feat: add brief v2 roster hint with v1 read compat"
```

### Task 7: Harness eval corpus berlabel plus ringkasan policy

**Berkas:**
- Buat: `evals/fixtures/participation-corpus.json`, `evals/test_participation_decider_eval.py`
- Tanpa ubah: `src/fakeyou/observability/report.py` (baris `policy_decided` dari Task 5 sudah membawa `decision_source`/`confidence`/`turn_cue; agregasi cukup di modul eval)

**Antarmuka:**
- Memakai: baris journal `policy_decided` plus `decision_source`, `confidence`, `turn_cue`
- Menghasilkan: `summarize_policy(rows)` (hitungan rule vs jev plus stale_pregen); `score_corpus(corpus, transport)` (`false_speak_rate`, `false_silent_rate`); fixture corpus berisi list `{id, text, participant_id, label: speak/silent/clarify, reason}`; eval menghitung rate tanpa network via transport palsu, Jev live di balik `-m live`

Perhatian hitungan: corpus dua baris (satu speak benar, satu silent salah jawab) menghasilkan `false_speak_rate == 1.0` dan `false_silent_rate == 0.0` (bukan 0.5); pembagi tiap rate adalah denominator labelnya masing-masing.

- [ ] **Langkah 1: Tulis tes merah**

```python
def test_corpus_harness_measures_false_speak_and_silent() -> None:
    from evals.test_participation_decider_eval import score_corpus

    corpus = [
        {
            "id": "c1",
            "text": "Harun, you're up",
            "participant_id": "a",
            "label": "speak",
            "reason": "explicit cue",
        },
        {
            "id": "c2",
            "text": "we will continue yesterday's items",
            "participant_id": "a",
            "label": "silent",
            "reason": "others conversing",
        },
    ]

    async def _speak(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "decision": {"choice": "speak", "confidence": 0.9, "probs": {}},
            "addressed_to_me": {"probability": 0.9},
            "others_conversing": {"probability": 0.0},
            "needs_clarify": {"probability": 0.0},
        }

    result = score_corpus(corpus, transport=_speak)
    assert result["false_speak_rate"] == 1.0
    assert result["false_silent_rate"] == 0.0


def test_report_summary_counts_rule_jev_and_stale() -> None:
    from evals.test_participation_decider_eval import summarize_policy

    rows = [
        {"event": "policy_decided", "decision_source": "rule", "reason": "rule_addressed"},
        {"event": "policy_decided", "decision_source": "jev", "reason": "jev_speak"},
        {"event": "policy_decided", "decision_source": "jev", "reason": "jev_stale_silent"},
    ]
    assert summarize_policy(rows) == {"rule": 1, "jev": 2, "stale_pregen": 1}
```

Teks inline tes wajib bebas kosakata Indonesia (aturan code-rules memindai `.py`, sedangkan fixture JSON boleh memuat ujaran Indonesia asli untuk pelabelan).

- [ ] **Langkah 2: Jalankan tes untuk memastikan merah**

Run: `uv run --no-sync pytest evals/test_participation_decider_eval.py -q`
Hasil: FAIL karena modul atau fungsi belum ada

- [ ] **Langkah 3: Tulis implementasi minimal**

`summarize_policy` menghitung `decision_source` plus alasan `jev_stale_silent`; `score_corpus` menjalankan `JevDecider` transport injeksi per baris corpus dan membagi tiap rate dengan denominator labelnya; `load_corpus` membaca fixture 12 baris (explicit cue, quote-not-cue, others-conversing, followup pindah penutur, overlap unknown, activation expired). Anotasi lengkap (`-> None`, `dict[str, Any]`) karena gate mypy strict berlaku untuk evals.

- [ ] **Langkah 4: Jalankan tes untuk memastikan hijau**

Run: `uv run --no-sync pytest evals/test_participation_decider_eval.py tests/test_turn_tracker.py tests/test_participation.py -q`
Hasil: PASS

- [ ] **Langkah 5: Commit**

```bash
git add evals/fixtures/participation-corpus.json evals/test_participation_decider_eval.py
git commit -m "test: add labeled participation corpus with false speak silent rates"
```
