# Rencana Implementasi Fase 1 Endpointing (Butir 16)

> **Untuk pekerja agentic:** SUB-SKILL WAJIB: pakai subagent-driven-development untuk mengerjakan plan ini task-per-task. Step memakai sintaks checkbox (`- [ ]`) untuk pelacakan.

**Goal:** Loop percakapan mengenali komplesi semantik dari interim dan mempra-generate respons sebelum final tiba; final yang sama persis mengadopsi hasil pra-generate (hemat latensi LLM), final berbeda membuang dan memakai jalur normal.

**Architecture:** `SemanticEndpoint` murni mendeteksi ujung semantik (tanda terminal + cukup kata + stabil antar-interim); `ConversationLoop` — bila flag `pre_generate_enabled` nyala dan sesi sepi — menjalankan LLM lebih awal ke buffer/antrean; final yang cocok kata-per-kata dinormalisasi mengadopsi (buffer langsung diucapkan, sisa stream dilanjut), final lain membuang via mesin cancel yang ada. Default MATI: perilaku live tak berubah.

**Tech Stack:** Python 3.12, pytest, ruff, mypy.

**Spec:** [long-term-architecture.md](long-term-architecture.md) butir 16 (L2/L3); rantai sebelumnya [phase-1-identity.md](phase-1-identity.md) (cabang basis `feat/phase-1-identity`, belum merge).

## Global Constraints

- Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia di `.py`; dokumentasi Bahasa Indonesia.
- Journal hanya metadata; tanpa teks transkrip, audio, prompt, atau secret.
- Event jurnal baru `pregeneration_started`/`pregeneration_finished` (akhiran `started`/`finished` ada di `EventVerb`); source `agent` konstan.
- Tanpa dependency baru; tanpa protokol privat Meet dan tanpa Meet Media API.
- Flag `FAKEYOU_CONVERSATION__PRE_GENERATE_ENABLED` default MATI; bila mati, nol perilaku baru (cabang pregen tak tersentuh) — pin dengan tes.
- Tes merah sebelum implementasi; reviewer terpisah per task; tanpa push/merge.
- Semua gate lama dipertahankan: suite serial hijau, browser hijau, `ruff check`, `ruff format --check`, `mypy`; tanpa `xfail`; tanpa tambah latensi agent di jalur normal (adopsi hanya memotong, tak menambah).
- Tes loop yang ada harus lulus TANPA MODIFIKASI sebagai bukti kompatibilitas.
- Verifikasi hanya offline; tanpa sesi live di plan ini.
- Progres dicatat di `.runtime/phase-1-endpointing/status.json` tiap task selesai.

---

## Scope Check

Butir 16 adalah subsistem terakhir Fase 1 (endpointing semantik + pra-generate). Di luar scope: endpointing berbasis model saraf (heuristikaturan kini; ambang dari data live nanti), adopsi longgar/fuzzy (adopsi menuntut cocok-persis ternormalisasi — aman konservatif), telemetri report (cukup jurnal; counter report saat flag dinyalakan).

## File Structure

| File | Tanggung jawab |
| --- | --- |
| `src/fakeyou/endpointing.py` (baru) | `SemanticEndpoint` murni (Task 1) |
| `src/fakeyou/conversation.py` | Mesin pra-generate di `ConversationLoop` (Task 2) |
| `src/fakeyou/settings.py` | `pre_generate_enabled` (Task 2) |
| `src/fakeyou/assembly.py` | Wiring flag ke loop (Task 2) |
| `tests/test_endpointing.py` (baru) | Tes endpointing (Task 1) |
| `tests/test_conversation.py` | Tes pra-generate (Task 2; tes lama tak tersentuh) |
| `docs/reports/2026-10-06-per-stream-acceptance.md` | Adendum implementasi (Task 3) |

---

### Task 1: SemanticEndpoint murni

**Files:**

- Create: `src/fakeyou/endpointing.py`
- Test: `tests/test_endpointing.py` (baru)

**Interfaces:**

- Consumes: `normalize_words` dari `fakeyou.utterance_quality` (tanpa siklus: modul itu daun stdlib).
- Produces: `TERMINAL_MARKS = frozenset({".", "?", "!"})`; `class SemanticEndpoint` dengan `__init__(min_words: int = 3)`, `observe(text: str) -> bool` (True hanya pada tepi-naik komplit), `reset() -> None`. Komplit = teks strip berakhir salah satu tanda terminal DAN `len(normalize_words(text)) >= min_words` DAN kata-ternormalisasi sama dengan interim sebelumnya DAN interim sebelumnya juga bertanda terminal (stabilitas penuh; perlu karena normalisasi mengupas tanda baca). Tepi-naik: True sekali per run-komplit; observasi tak-komplit mengakhiri run (komplit berikutnya True lagi).

- [ ] **Step 1: tulis tes merah**

```python
from fakeyou.endpointing import SemanticEndpoint


def test_endpoint_fires_once_per_stable_completion() -> None:
    endpoint = SemanticEndpoint()
    assert endpoint.observe("are you coming") is False
    assert endpoint.observe("are you coming today") is False
    assert endpoint.observe("are you coming today?") is False
    assert endpoint.observe("are you coming today?") is True
    assert endpoint.observe("are you coming today?") is False


def test_endpoint_ignores_short_and_unstable_text() -> None:
    endpoint = SemanticEndpoint()
    assert endpoint.observe("ok?") is False
    assert endpoint.observe("ok?") is False
    endpoint.reset()
    assert endpoint.observe("please open the door.") is False
    assert endpoint.observe("please open the hatch.") is False
    assert endpoint.observe("please open the hatch.") is True
```

- [ ] **Step 2: jalankan tes, pastikan merah**

Run: `pytest tests/test_endpointing.py -v`
Expected: FAIL (`endpointing` belum ada)

- [ ] **Step 3: implementasi minimal**

```python
from fakeyou.utterance_quality import normalize_words

TERMINAL_MARKS = frozenset({".", "?", "!"})


class SemanticEndpoint:
    def __init__(self, min_words: int = 3) -> None:
        self._min_words = min_words
        self._previous: tuple[str, ...] | None = None
        self._previous_terminal = False
        self._fired = False

    def reset(self) -> None:
        self._previous = None
        self._previous_terminal = False
        self._fired = False

    def observe(self, text: str) -> bool:
        words = normalize_words(text)
        terminal = text.strip().endswith(tuple(TERMINAL_MARKS))
        complete = (
            terminal
            and len(words) >= self._min_words
            and self._previous is not None
            and self._previous_terminal
            and words == self._previous
        )
        self._previous = words
        self._previous_terminal = terminal
        if not complete:
            self._fired = False
            return False
        if self._fired:
            return False
        self._fired = True
        return True
```

(`str.endswith` menerima tuple; tak perlu impor tambahan.)

- [ ] **Step 4: jalankan tes, pastikan hijau**

Run: `pytest tests/test_endpointing.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: review reviewer terpisah, lalu commit**

```bash
git add src/fakeyou/endpointing.py tests/test_endpointing.py
git commit -m "feat(l3): detect semantic utterance completion"
```

---

### Task 2: mesin pra-generate di ConversationLoop

**Files:**

- Modify: `src/fakeyou/conversation.py` (state pregen, cabang interim/final, adopsi/buang)
- Modify: `src/fakeyou/settings.py` (`ConversationSettings.pre_generate_enabled: bool = False`)
- Modify: `src/fakeyou/assembly.py` (teruskan flag ke `ConversationLoop`)
- Test: `tests/test_conversation.py` (tambah; tes lama tak tersentuh)

**Interfaces:**

- Consumes: `SemanticEndpoint` (Task 1), `normalize_words`, `StreamingConversationResponder`, `TranscriptEvent`.
- Produces: `ConversationLoop(..., pre_generate_enabled: bool = False)`; interim komplit saat sepi → task buffer `responder.stream_sentences` (kalimat ke `buffered`, jurnal `pregeneration_started`); final cocok-persis ternormalisasi → adopsi (buffer langsung `session.speak` berindeks, sisa antrean diucapkan worker adopsi, jurnal `pregeneration_finished` outcome `adopted`); final lain / tolak-gate / tutup → buang (cancel task, jurnal outcome `discarded` + reason `mismatched|rejected|error|expired`); buffer dibungkus `asyncio.timeout(response_timeout)`; flag mati → nol perilaku baru.

Kontrak tepi (pin di tes): satu pregen aktif per waktu; adopsi menuntut responder streaming + `sentence_streaming` (ikuti syarat `_run_turn`); adopsi mendaftarkan worker sebagai `_active_turn` (barge-in/cancel tetap bekerja); id interim `pregen-{n}`, request `{id}:{generation}`; adopsi menaikkan `_generation` dan mengosongkan `_pending` (final adopsi tak masuk antre normal); tutup loop membuang pregen.

- [ ] **Step 1: tulis tes merah**

```python
class _StreamingResponder:
    def __init__(self, sentences: list[str]) -> None:
        self.sentences = sentences
        self.calls: list[list[TranscriptEvent]] = []
        self.released = asyncio.Event()

    def stream_sentences(self, events, on_tool_start, *, request_id: str):
        self.calls.append(list(events))
        return self._generate()

    async def _generate(self):
        for sentence in self.sentences:
            yield sentence


async def test_pregeneration_adopts_exact_match_without_second_call(journal) -> None:
    responder = _StreamingResponder(["Yes I can.", "Let me check."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_transcript(event("f1", "can you help me today?", finalized=True))
    await loop.wait_idle()
    assert audio.spoken == ["Yes I can.", "Let me check."]
    assert len(responder.calls) == 1
    assert journal_has(journal, "pregeneration_finished", outcome="adopted")


async def test_pregeneration_discards_on_mismatch_and_uses_normal_path(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    await loop.handle_transcript(event("f1", "cancel everything now", finalized=True))
    await loop.wait_idle()
    assert len(responder.calls) == 2
    assert journal_has(journal, "pregeneration_finished", outcome="discarded")


async def test_pregeneration_stays_off_by_default(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(responder, journal=journal, sentence_streaming=True)
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    assert responder.calls == []
    await loop.handle_transcript(event("f1", "can you help me today?", finalized=True))
    await loop.wait_idle()
    assert len(responder.calls) == 1
```

(`event`, `FakeReplyAudio`, `journal` mengikuti helper/fixture yang sudah ada di berkas tes; `journal_has` helper baca jurnal mengikuti pola asersi jurnal yang ada — sesuaikan nama dengan helper aktual.)

- [ ] **Step 2: jalankan tes, pastikan merah**

Run: `pytest tests/test_conversation.py::test_pregeneration_adopts_exact_match_without_second_call tests/test_conversation.py::test_pregeneration_discards_on_mismatch_and_uses_normal_path tests/test_conversation.py::test_pregeneration_stays_off_by_default -v`
Expected: FAIL (`pre_generate_enabled` belum ada)

- [ ] **Step 3: implementasi minimal**

State baru di `__init__` + `close()` membuang pregen. Holder privat:

```python
@dataclass(slots=True)
class _Pregeneration:
    words: tuple[str, ...]
    request_id: str
    started: float
    generation: int
    task: asyncio.Task[None]
    queue: asyncio.Queue[str | None]
    buffered: list[str]
    adopted: bool = False
```

`handle_interim`: baris pertama `await self._observe_pregen(text)`; sisa fungsi tak tersentuh.

`_observe_pregen(text)`: pulang bila tutup/gagal/session-None/mati-flag/responder-bukan-streaming/`sentence_streaming` mati/sibuk (`is_busy`, `_active_turn` hidup, `_pending` tak kosong, `session.is_speaking`)/pregen-aktif/`endpoint.observe(text)` False. Else: bangun interim `TranscriptEvent(f"pregen-{n}", now-iso, "meeting_audio", text, False, "unknown", None)`, catat `words=normalize_words(text)`, buat `_Pregeneration` + task `_buffer_pregen`, jurnal `pregeneration_started` (request_id, words=len).

`_buffer_pregen(pregen, events)`: `async with asyncio.timeout(self._response_timeout)` di sekeliling `async for sentence in responder.stream_sentences(...)`: bila `pregen.adopted` → `queue.put_nowait(sentence)` else `buffered.append`. `finally`: bila adopted → `put_nowait(None)`. Timeout/error → biarkan eksepsi mengakhiri task (adopsi memeriksa).

`handle_transcript`: setelah cek tutup/gagal/final/blank/dup (sebelum gate policy), tambah `self._endpoint.reset()`. Di tiap dari 3 situs tolak-gate (policy, wake, bare_ack_run) sebelum `return`, tambah `await self._discard_pregen("rejected")`. Tepat sebelum `self._pending.append(event)`, tambah:

```python
        if await self._maybe_adopt_pregen(event):
            return
```

`_maybe_adopt_pregen(event) -> bool`: pregen None → False. Task gagal/error → buang (`error`), False. `normalize_words(event.text) != pregen.words` → buang (`mismatched`), False. Else adopsi:
  1. `pregen.adopted = True`; `self._generation += 1`; `self._pending.clear()`
  2. `request_id = f"{event.id}:{self._generation}"`; jurnal `pregeneration_finished` (request_id, outcome `adopted`, duration_ms)
  3. ucapkan buffer: `for index, sentence in enumerate(pregen.buffered): await session.speak(sentence, sentence_index=index, transcript_id=event.id)`; bila buffer tak kosong `_move("queued", event.id)`
  4. daftarkan worker adopsi sebagai `_active_turn` (+ id/request/generation/started seperti `_respond_pending`); worker `_consume_adopted(pregen, event, request_id, generation, started, offset=len(buffered))`: dalam `asyncio.timeout(response_timeout)`: `while True: sentence = await queue.get(); if None: break; cek tutup/generation/stop (cermin `_speak_streamed`: jurnal `sentence_dropped` + `agent_cancelled`, return); await session.speak(...)`; selesai → jurnal `agent_finished` + `_move("listening")`. `finally`: tutup antrean/stream (adopsi tak memiliki stream — buffer task yang `aclose`? cerminkan `finally` `_speak_streamed`: getattr stream aclose — buffer task menyimpan referensi stream di holder untuk penutupan; terapkan pola yang sama di kedua sisi).
  5. `return True`

`_discard_pregen(reason)`: pregen None → pulang; set None dulu; cancel task + gather; jurnal `pregeneration_finished` (request_id pregen, outcome `discarded`, reason). Timeout task buffer yang kadaluwarsa sebelum final: adopsi melihat task done + eksepsi → `error`... bedakan TimeoutError → reason `expired`. (Esekutor: `task.exception()`; TimeoutError → expired else error.)

`settings.py`: `pre_generate_enabled: bool = False` di `ConversationSettings`. `assembly.py`: teruskan `pre_generate_enabled=settings.conversation.pre_generate_enabled` di konstruksi loop.

- [ ] **Step 4: jalankan tes, pastikan hijau**

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/test_conversation.py tests/test_endpointing.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: review reviewer terpisah, lalu commit**

```bash
git add src/fakeyou/conversation.py src/fakeyou/settings.py src/fakeyou/assembly.py tests/test_conversation.py
git commit -m "feat(l3): pre-generate replies from semantic interim"
```

---

### Task 3: gate penuh + adendum + status

**Files:**

- Modify: `docs/reports/2026-10-06-per-stream-acceptance.md` (adendum implementasi)
- Modify: `.runtime/phase-1-endpointing/status.json` (final; untracked)
- Test: tanpa tes baru; verifikasi gate penuh.

**Interfaces:**

- Consumes: Task 1–2.
- Produces: adendum laporan + status final + cabang siap review merge (tanpa push/merge) + Fase 1 lengkap.

- [ ] **Step 1: gate serial penuh**

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/ -q -p no:cacheprovider`
Expected: exit 0, 0 failed (catat angka passed/skipped/deselected aktual)

- [ ] **Step 2: gate statis + browser**

Run: `ruff check .`, `ruff format --check .`, `mypy src`, browser `-m "browser"` dengan override addopts agar tes browser benar jalan (catat output aktual)
Expected: bersih semua

- [ ] **Step 3: tulis adendum + status, commit**

Adendum: tabel butir 16, angka gate aktual, penegasan default-mati + adopsi-persis + tak ada perubahan jalur normal (tes lama hijau tak tersentuh), Fase 1 (butir 11–16) lengkap + peta cabang bertumpuk menuju merge. Status: task, status tiap task, command + exit code, artefak, gate terbuka (Fase 2 / ukur live adopsi).

```bash
git add docs/reports/2026-10-06-per-stream-acceptance.md
git commit -m "docs(phase-1): endpointing implementation addendum and status"
```

---

## Self-Review

1. **Cakupan spec:** butir 16 → Task 1 (endpointing semantik client-side: komplesi vs hening) + Task 2 (pra-generate dari interim + adopsi/buang). Best practice "pisahkan endpointing dari utterance_end" terwujud sebagai sinyal tersendiri; "lapisi endpointing semantik" terwujud sebagai gerbang pra-generate.
2. **Pindai placeholder:** tiap step berisi kode/perintah aktual; helper tes dirujuk ke pola helper yang sudah ada.
3. **Konsistensi tipe:** `normalize_words -> tuple[str, ...]` dipakai di endpoint + cocok-adopsi; `_Pregeneration.words: tuple[str, ...]`; antrean `str | None` (None = akhir); event jurnal `pregeneration_started/finished` + outcome/reason.
