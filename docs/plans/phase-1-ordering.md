# Rencana Implementasi Fase 1 Ordering (Butir 11 + 12)

> **Untuk pekerja agentic:** SUB-SKILL WAJIB: pakai subagent-driven-development untuk mengerjakan plan ini task-per-task. Step memakai sintaks checkbox (`- [ ]`) untuk pelacakan.

**Goal:** Seq + jam capture mengalir ujung-ke-ujung dari drain JS sampai record, dan bacaan record memperbaiki urutan transkrip dari jam capture tanpa mengubah jalur live.

**Architecture:** Drain JS menomori tiap slot secara monoton; Python memvalidasi gap/duplikat dan mencatat `drain_gap_detected`; jam capture (page_ms) dicap ke frame lalu ke `TranscriptEvent` dan `TranscriptEntry`; live merge tetap publish-segera (nol tambah latensi); `load_meeting_record` memperbaiki urutan saat baca.

**Tech Stack:** Python 3.12, JavaScript in-page (WebRTC standar saja), pytest + Playwright, ruff, mypy.

**Spec:** [long-term-architecture.md](long-term-architecture.md) butir 11 (L1) dan 12 (L3); keputusan butir 12 = record-repair tanpa holdback (0/256 inversi, adendum overlap probe di [2026-10-06-per-stream-acceptance.md](../reports/2026-10-06-per-stream-acceptance.md)); kontrak global di [per-stream-capture.md](per-stream-capture.md).

## Global Constraints

- Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia di `.py`; dokumentasi Bahasa Indonesia.
- Journal hanya metadata; tanpa teks transkrip, audio, prompt, atau secret.
- Tanpa dependency baru; tanpa protokol privat Meet dan tanpa Meet Media API; WebRTC standar saja.
- Flag `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED` default tetap mati; plan ini tidak mengubah flag.
- Tes merah sebelum implementasi; reviewer terpisah per task; tanpa push/merge.
- Semua gate lama dipertahankan: suite serial hijau, browser hijau, `ruff check`, `ruff format --check`, `mypy`; tanpa `xfail`; tanpa tambah latensi agent (jalur live tidak berubah).
- Verifikasi hanya offline + browser harness; tanpa sesi live di plan ini.
- Progres dicatat di `.runtime/phase-1-ordering/status.json` tiap task selesai.

---

## Scope Check

Fase 1 butir 11–16 adalah empat subsistem independen. Plan ini hanya mencakup subsistem ordering (butir 11 + 12: L1 transport clock + L3 record-repair) karena menghasilkan perangkat lunak yang bekerja dan teruji sendiri. Butir 13–14 (supervisor tap + pre-roll/keepalive), 15 (tabel fusi identitas), 16 (endpointing semantik) masing-masing dapat plan tersendiri sesudah ini merge.

## File Structure

| File | Tanggung jawab |
| --- | --- |
| `src/fakeyou/assets/stream_capture.js` | `drain()` mengemisikan `seq` monoton per slot (Task 1) |
| `src/fakeyou/stream_capture.py` | `StreamChunk.seq`, validasi seq + jurnal `drain_gap_detected`, `note_capture`/`last_capture`, journal ke registry (Task 2–3) |
| `src/fakeyou/audio.py` | `_tagged` mencap `capture_seq`/`capture_ms` ke frame via provider jam (Task 3) |
| `src/fakeyou/conversation.py` | `TranscriptEvent.capture_ms` + param `transcript_event` (Task 3) |
| `src/fakeyou/cli/join.py` | `attribute_stream_frame` 5-tuple + teruskan `capture_ms` (Task 3) |
| `src/fakeyou/record.py` | `TranscriptEntry.capture_ms`, `repaired_order`, `load_meeting_record(repair=True)` (Task 3–4) |
| `src/fakeyou/report.py` | hitung `drain_gaps` di `stt_quality` (Task 2) |
| `tests/test_stream_capture.py` | tes merah/validasi Task 1–3 |
| `tests/test_report.py` | tes hitung `drain_gaps` (Task 2) |
| `tests/test_record.py` | tes repair-on-read (Task 4) |
| `docs/reports/2026-10-06-per-stream-acceptance.md` | adendum implementasi butir 11+12 (Task 5) |

---

### Task 1: seq monoton per slot di drain JS

**Files:**
- Modify: `src/fakeyou/assets/stream_capture.js:15-31` (tambah counter)
- Modify: `src/fakeyou/assets/stream_capture.js:609-650` (emisikan `seq`)
- Test: `tests/test_stream_capture.py` (tambah tes browser)

**Interfaces:**
- Consumes: tidak ada (awal rantai).
- Produces: tiap entri `streams` dari `drain()` membawa `seq: int`, mulai 0 per slot per page load, naik 1 tiap drain di mana slot itu muncul.

- [ ] **Step 1: tulis tes merah**

```python
@pytest.mark.browser
async def test_stream_capture_drain_emits_monotonic_seq_per_slot() -> None:
    script = load_capture_script()
    async with open_direct_page(headless=True) as page:
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(1.5)
        first = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await asyncio.sleep(0.5)
        second = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    first_seq = {entry["slot"]: entry["seq"] for entry in first["streams"]}
    second_seq = {entry["slot"]: entry["seq"] for entry in second["streams"]}
    assert first_seq == {0: 0, 1: 0}
    assert second_seq == {0: 1, 1: 1}
```

- [ ] **Step 2: jalankan tes, pastikan merah**

Run: `pytest tests/test_stream_capture.py::test_stream_capture_drain_emits_monotonic_seq_per_slot -v`
Expected: FAIL dengan `KeyError: 'seq'`

- [ ] **Step 3: implementasi minimal**

Di `stream_capture.js` setelah baris 24 (`const encodedAtBySlot = new Map();`), tambah:

```js
const seqBySlot = new Map();
```

Di `drain()`, dalam `.map(([slot, list]) => {`, sebelum `return {`, tambah:

```js
        const seq = seqBySlot.has(slot) ? seqBySlot.get(slot) + 1 : 0;
        seqBySlot.set(slot, seq);
```

dan tambah field `seq,` ke objek return:

```js
        return {
          slot,
          seq,
          csrcs,
          level,
          page_ms,
          pcm_b64: encodeBase64(new Uint8Array(pcm.buffer)),
          track,
          engine: want,
        };
```

- [ ] **Step 4: jalankan tes, pastikan hijau**

Run: `pytest tests/test_stream_capture.py -q -p no:cacheprovider -k "browser or seq or drain"`
Expected: PASS

- [ ] **Step 5: review reviewer terpisah, lalu commit**

```bash
git add src/fakeyou/assets/stream_capture.js tests/test_stream_capture.py
git commit -m "feat(l1): emit monotonic per-slot seq from stream drain"
```

---

### Task 2: parse + validasi seq, jurnal drain_gap_detected, hitung di report

**Files:**
- Modify: `src/fakeyou/stream_capture.py:64-70` (`StreamChunk.seq`), `:73-81` (`StreamSession.seq`), `:88-123` (`_parse_entry`), `:163-176` (`StreamRegistry.__init__` journal), `:202-213` (`feed` validasi), `:444` (`open_stream_fleet` teruskan journal)
- Modify: `src/fakeyou/report.py` (init `drain_gaps = 0`, cabang `elif event == "drain_gap_detected"`, output `"drain_gaps": drain_gaps` di `stt_quality`)
- Test: `tests/test_stream_capture.py`, `tests/test_report.py`

**Interfaces:**
- Consumes: entri drain membawa `seq` (Task 1).
- Produces: `StreamChunk.seq: int`; `_parse_entry` menolak seq bukan-int/bool/negatif dengan `MeetingAudioError("stream drain window is corrupt: seq is not a valid integer")`; `StreamRegistry.feed` mencatat jurnal `drain_gap_detected` (source `transport`, field `slot`, `kind` = `gap`|`resync`, `expected`, `received`, `missed`) saat seq lompat (`received > expected`, `missed = received - expected`) atau mundur/berulang (epoch baru, diterima); `build_session_report` mengeluarkan `stt_quality.drain_gaps: int`.

- [ ] **Step 1: tulis tes merah**

```python
def test_parse_drain_rejects_bad_seq() -> None:
    good = {
        "slot": 0,
        "seq": 0,
        "csrcs": [],
        "level": 0.1,
        "page_ms": 10.0,
        "pcm_b64": base64.b64encode(b"\x00\x00").decode(),
    }
    assert parse_drain({"streams": [good]})[0].seq == 0
    for bad in ("1", 1.5, True, -1, None):
        with pytest.raises(MeetingAudioError, match="seq is not a valid integer"):
            parse_drain({"streams": [{**good, "seq": bad}]})
    with pytest.raises(MeetingAudioError, match="seq is not a valid integer"):
        parse_drain({"streams": [{k: v for k, v in good.items() if k != "seq"}]})


async def test_registry_feed_journals_drain_gap_detected_and_resync() -> None:
    journal = Journal()
    registry, _ = _registry(journal=journal)
    chunk = _chunk(slot=0, seq=0)
    await registry.feed(chunk)
    await registry.feed(_chunk(slot=0, seq=2))
    await registry.feed(_chunk(slot=0, seq=0))
    gaps = [row for row in journal.rows if row["event"] == "drain_gap_detected"]
    assert [(row["kind"], row["missed"]) for row in gaps] == [("gap", 1), ("resync", 0)]
```

(`_registry`/`_chunk` helper lokal mengikuti pola helper yang sudah ada di berkas tes: factory registry dengan `monotonic` terkendali + `_RecordingInput`; tambah param `journal` yang diteruskan ke `StreamRegistry`. `Journal.rows` mengikuti API journal yang dipakai tes report yang ada; sesuaikan nama akses baris dengan helper tes yang sudah ada.)

Tes report:

```python
def test_session_report_counts_drain_gaps() -> None:
    rows = [
        {"event": "drain_gap_detected", "slot": 0, "kind": "gap", "missed": 2},
        {"event": "drain_gap_detected", "slot": 1, "kind": "resync", "missed": 0},
    ]
    assert build_session_report(rows)["stt_quality"]["drain_gaps"] == 2
```

- [ ] **Step 2: jalankan tes, pastikan merah**

Run: `pytest tests/test_stream_capture.py::test_parse_drain_rejects_bad_seq tests/test_stream_capture.py::test_registry_feed_journals_drain_gap_detected_and_resync tests/test_report.py::test_session_report_counts_drain_gaps -v`
Expected: FAIL (seq belum di-parse; `drain_gap_detected` belum dicatat; kunci `drain_gaps` belum ada)

- [ ] **Step 3: implementasi minimal**

`StreamChunk` tambah field `seq: int` setelah `slot`; `StreamSession` tambah `seq: int = -1` (-1 = epoch belum mulai). `_parse_entry` setelah validasi slot:

```python
    seq = entry.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise _drain_error("seq is not a valid integer")
```

dan teruskan `seq=seq` ke konstruktor `StreamChunk`.

`StreamRegistry.__init__` tambah param `journal: Journal | None = None`, simpan `self._journal = journal`. `feed` setelah blok `session is None`, sebelum update field:

```python
        expected = session.seq + 1
        if session.seq >= 0 and chunk.seq != expected and self._journal is not None:
            if chunk.seq > expected:
                self._journal.record(
                    "drain_gap_detected",
                    source="transport",
                    slot=chunk.slot,
                    kind="gap",
                    expected=expected,
                    received=chunk.seq,
                    missed=chunk.seq - expected,
                )
            else:
                self._journal.record(
                    "drain_gap_detected",
                    source="transport",
                    slot=chunk.slot,
                    kind="resync",
                    expected=expected,
                    received=chunk.seq,
                    missed=0,
                )
        session.seq = chunk.seq
```

`open_stream_fleet`: `registry = StreamRegistry(binder=binder, journal=journal)`.

`report.py`: tambah `drain_gaps = 0` di samping `order_inversions = 0`; tambah cabang `elif event == "drain_gap_detected": drain_gaps += 1` di samping cabang inversi; tambah `"drain_gaps": drain_gaps,` di dict `stt_quality` di samping `order_inversions`.

- [ ] **Step 4: jalankan tes, pastikan hijau**

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/test_stream_capture.py tests/test_report.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: review reviewer terpisah, lalu commit**

```bash
git add src/fakeyou/stream_capture.py src/fakeyou/report.py tests/test_stream_capture.py tests/test_report.py
git commit -m "feat(l1): validate drain seq and journal drain gaps"
```

---

### Task 3: jam capture ujung-ke-ujung sampai record

**Files:**
- Modify: `src/fakeyou/stream_capture.py:38-62` (`StreamAudioInput.note_capture`/`last_capture`), `:202-213` (`feed` mencatat jam)
- Modify: `src/fakeyou/audio.py:778-785` (`_tagged` provider jam), `:789-813`, `:850` (teruskan provider dari `input`)
- Modify: `src/fakeyou/conversation.py:41-49` (`TranscriptEvent.capture_ms`), `:52-72` (param `transcript_event`)
- Modify: `src/fakeyou/cli/join.py:83-101` (`attribute_stream_frame` 5-tuple), `:177-178` (teruskan `capture_ms`)
- Modify: `src/fakeyou/record.py:22-30` (`TranscriptEntry.capture_ms`), `:84-99` (simpan)
- Test: `tests/test_stream_capture.py`, `tests/test_conversation.py`, `tests/test_record.py`

**Interfaces:**
- Consumes: `StreamChunk.seq` + `page_ms` (Task 2).
- Produces: `StreamAudioInput.note_capture(seq: int, capture_ms: float) -> None` dan properti `last_capture: tuple[int, float] | None`; `_tagged(stream_id, callback, clock=None)` dengan `clock: Callable[[], tuple[int, float] | None] | None`, mencap `frame.capture_seq`/`frame.capture_ms` saat provider mengembalikan nilai; `attribute_stream_frame(frame, binder) -> tuple[str, str | None, str, dict[str, Any], float]` (elemen ke-5 = capture_ms, 0.0 bila tak ada/bukan angka); `transcript_event(..., capture_ms: float = 0.0)`; `TranscriptEvent.capture_ms: float = 0.0` (tidak masuk hash id); `TranscriptEntry.capture_ms: float = 0.0`; `MeetingRecord.add_transcript` menyimpan `event.capture_ms`. `merge_transcripts` TIDAK berubah.

- [ ] **Step 1: tulis tes merah**

```python
async def test_registry_feed_stamps_capture_clock_on_input() -> None:
    registry, made = _registry()
    await registry.feed(_chunk(slot=0, seq=4, page_ms=1234.5))
    assert made[0].last_capture == (4, 1234.5)


async def test_tagged_callback_stamps_capture_clock() -> None:
    seen: list[TranscriptionFrame] = []

    async def record(frame: TranscriptionFrame) -> None:
        seen.append(frame)

    await _tagged("slot-0", record, clock=lambda: (7, 99.5))(frame("hi"))
    assert (seen[0].capture_seq, seen[0].capture_ms) == (7, 99.5)
    await _tagged("slot-0", record)(frame("hi"))
    assert getattr(seen[1], "capture_ms", 0.0) == 0.0


def test_attribute_stream_frame_returns_capture_ms() -> None:
    stamped = frame("hi")
    stamped.transport_source = "slot-2"
    stamped.capture_ms = 55.5
    assert attribute_stream_frame(stamped, SlotBinder(capacity=4))[4] == 55.5
    plain = frame("hi")
    plain.transport_source = "slot-2"
    assert attribute_stream_frame(plain, SlotBinder(capacity=4))[4] == 0.0


def test_transcript_event_and_record_carry_capture_ms(tmp_path: Path) -> None:
    event = transcript_event(
        frame("hi", "2026-10-06T01:00:01+00:00"), "a", None, "s0", capture_ms=55.5
    )
    assert event.capture_ms == 55.5
    assert (
        transcript_event(frame("hi", "2026-10-06T01:00:01+00:00"), "a", None, "s0").capture_ms
        == 0.0
    )
    record = MeetingRecord(tmp_path / "record.jsonl")
    record.add_transcript(event)
    record.close()
    assert load_meeting_record(tmp_path / "record.jsonl", repair=False)[0].capture_ms == 55.5
```

(`frame(...)` = konstruktor frame STT tiruan yang sudah dipakai helper tes; `_RecordingInput` di `tests/test_stream_capture.py` perlu properti `last_capture` + metode `note_capture` yang sama persis dengan produksi agar tes registry valid — tambah keduanya di helper tes sebagai bagian tes merah.)

- [ ] **Step 2: jalankan tes, pastikan merah**

Run: `pytest tests/test_stream_capture.py::test_registry_feed_stamps_capture_clock_on_input tests/test_stream_capture.py::test_tagged_callback_stamps_capture_clock tests/test_stream_capture.py::test_attribute_stream_frame_returns_capture_ms tests/test_conversation.py::test_transcript_event_and_record_carry_capture_ms -v`
Expected: FAIL (`note_capture`/`last_capture`/`capture_ms` belum ada)

- [ ] **Step 3: implementasi minimal**

`StreamAudioInput.__init__` tambah `self._capture: tuple[int, float] | None = None`; tambah:

```python
def note_capture(self, seq: int, capture_ms: float) -> None:
    self._capture = (seq, capture_ms)


@property
def last_capture(self) -> tuple[int, float] | None:
    return self._capture
```

`feed` tambah sebelum `await session.input.feed(chunk.pcm)`:

```python
        session.input.note_capture(chunk.seq, chunk.page_ms)
```

`_tagged`:

```python
def _tagged(
    stream_id: str,
    callback: Callable[[TranscriptionFrame], Awaitable[None]],
    clock: Callable[[], tuple[int, float] | None] | None = None,
) -> Callable[[TranscriptionFrame], Awaitable[None]]:
    async def tagged(frame: TranscriptionFrame) -> None:
        frame.transport_source = stream_id
        stamped = clock() if clock is not None else None
        if stamped is not None:
            frame.capture_seq = stamped[0]
            frame.capture_ms = stamped[1]
        await callback(frame)

    return tagged
```

`open_stream_audio`: di kedua situs `_tagged` (interim `:813` dan transkrip `:850`), teruskan `clock=lambda: getattr(input, "last_capture", None)` (duck-typing; tanpa import baru, tanpa siklus import).

`TranscriptEvent` tambah `capture_ms: float = 0.0` setelah `stream_id`; `transcript_event` tambah param keyword `capture_ms: float = 0.0` dan teruskan ke konstruktor; hash identitas TIDAK menyertakan `capture_ms`.

`attribute_stream_frame`: di awal fungsi tambah:

```python
    raw_capture = getattr(frame, "capture_ms", 0.0)
    capture_ms = (
        float(raw_capture)
        if isinstance(raw_capture, (int, float)) and not isinstance(raw_capture, bool)
        else 0.0
    )
```

dan tiap `return` 4-tuple menjadi 5-tuple dengan `capture_ms` di ujung. `join.py:177-178` menjadi:

```python
participant_id, evidence_id, stream_id, extra, capture_ms = attribute_stream_frame(frame, binder)
event = transcript_event(frame, participant_id, evidence_id, stream_id, capture_ms=capture_ms)
```

`TranscriptEntry` tambah `capture_ms: float = 0.0`; `add_transcript` teruskan `capture_ms=event.capture_ms`.

- [ ] **Step 4: jalankan tes, pastikan hijau**

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/test_stream_capture.py tests/test_conversation.py tests/test_record.py tests/test_audio_stt_reconnect.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: review reviewer terpisah, lalu commit**

```bash
git add src/fakeyou/stream_capture.py src/fakeyou/audio.py src/fakeyou/conversation.py src/fakeyou/cli/join.py src/fakeyou/record.py tests/test_stream_capture.py tests/test_conversation.py tests/test_record.py
git commit -m "feat(l1): stamp capture clock end to end into record"
```

---

### Task 4: repair-on-read di MeetingRecord (butir 12)

**Files:**
- Modify: `src/fakeyou/record.py:61-66` (`load_meeting_record` param repair + fungsi `repaired_order`)
- Test: `tests/test_record.py`

**Interfaces:**
- Consumes: `TranscriptEntry.capture_ms` (Task 3; 0.0 = tak dicap: jalur mixed + record lama).
- Produces: `repaired_order(entries: Sequence[RecordEntry]) -> list[RecordEntry]` — murni, tak mutasi input; transkrip tercap (`capture_ms > 0`) diurut stabil by `(capture_ms, spoken_at, id)` lalu ditulis kembali ke posisi-posisi tercap; semua entri lain (tak tercap + non-transkrip) tetap di tempat. `load_meeting_record(path, *, repair: bool = True)` mengembalikan urutan repair; `repair=False` untuk forensik urutan-bita.

- [ ] **Step 1: tulis tes merah**

```python
def test_repaired_order_sorts_stamped_transcripts_in_place(tmp_path: Path) -> None:
    record = MeetingRecord(tmp_path / "record.jsonl")
    late = transcript_event(
        frame("two", "2026-10-06T01:00:02+00:00"), "b", None, "s1", capture_ms=200.0
    )
    early = transcript_event(
        frame("one", "2026-10-06T01:00:01+00:00"), "a", None, "s0", capture_ms=100.0
    )
    record.add_transcript(late)
    record.add_twin_speech("ack")
    record.add_transcript(early)
    record.add_transcript(
        transcript_event(frame("old", "2026-10-06T01:00:00+00:00"), "a", None, "s0")
    )
    record.close()
    repaired = load_meeting_record(tmp_path / "record.jsonl")
    assert [entry.kind for entry in repaired] == [
        "transcript",
        "twin_speech",
        "transcript",
        "transcript",
    ]
    assert [entry.text for entry in repaired if entry.kind == "transcript"] == ["one", "two", "old"]
    assert repaired[1].text == "ack"
    raw = load_meeting_record(tmp_path / "record.jsonl", repair=False)
    assert [entry.text for entry in raw if entry.kind == "transcript"] == ["two", "one", "old"]


def test_repaired_order_is_stable_and_pure() -> None:
    first = TranscriptEntry(
        at=datetime.now(UTC),
        id="a",
        spoken_at="t",
        source="s",
        text="x",
        participant_id="p",
        speaker_evidence_id=None,
        stream_id="s0",
        capture_ms=10.0,
    )
    second = TranscriptEntry(
        at=datetime.now(UTC),
        id="b",
        spoken_at="t",
        source="s",
        text="y",
        participant_id="p",
        speaker_evidence_id=None,
        stream_id="s1",
        capture_ms=10.0,
    )
    entries = [second, first]
    assert [entry.id for entry in repaired_order(entries)] == ["a", "b"]
    assert [entry.id for entry in entries] == ["b", "a"]
```

(tie `capture_ms` sama pecah by `spoken_at` lalu `id`: deterministik penuh.)

- [ ] **Step 2: jalankan tes, pastikan merah**

Run: `pytest tests/test_record.py::test_repaired_order_sorts_stamped_transcripts_in_place tests/test_record.py::test_repaired_order_is_stable_and_pure -v`
Expected: FAIL (`repaired_order` belum ada; kwarg `repair` belum ada)

- [ ] **Step 3: implementasi minimal**

```python
def repaired_order(entries: Sequence[RecordEntry]) -> list[RecordEntry]:
    positions = [
        index
        for index, entry in enumerate(entries)
        if isinstance(entry, TranscriptEntry) and entry.capture_ms > 0.0
    ]
    stamped = sorted(
        (entries[index] for index in positions),
        key=lambda entry: (
            entry.capture_ms,
            entry.spoken_at,
            entry.id,
        ),
    )
    fixed = list(entries)
    for index, entry in zip(positions, stamped, strict=True):
        fixed[index] = entry
    return fixed


def load_meeting_record(path: Path, *, repair: bool = True) -> list[RecordEntry]:
    entries = [
        _RECORD_ENTRY.validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return repaired_order(entries) if repair else entries
```

(`sorted` Python stabil; kunci penuh `(capture_ms, spoken_at, id)` deterministik. `zip(..., strict=True)` aman karena panjang sama by konstruksi.)

- [ ] **Step 4: jalankan tes, pastikan hijau**

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/test_record.py tests/test_conversation.py -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: review reviewer terpisah, lalu commit**

```bash
git add src/fakeyou/record.py tests/test_record.py
git commit -m "feat(l3): repair transcript order on record read"
```

---

### Task 5: gate penuh + adendum + status

**Files:**
- Modify: `docs/reports/2026-10-06-per-stream-acceptance.md` (adendum implementasi)
- Modify: `.runtime/phase-1-ordering/status.json` (final)
- Test: tanpa tes baru; verifikasi gate penuh.

**Interfaces:**
- Consumes: Task 1–4.
- Produces: adendum laporan + status final + cabang siap review merge (tanpa push/merge).

- [ ] **Step 1: gate serial penuh**

Run: `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false pytest tests/ -q -p no:cacheprovider`
Expected: exit 0, 0 failed (catat angka passed/skipped/deselected aktual)

- [ ] **Step 2: gate statis**

Run: `ruff check .`, `ruff format --check .`, `mypy src` (atau perintah gate repo yang berlaku; catat output aktual)
Expected: bersih semua

- [ ] **Step 3: tulis adendum + status, commit**

Adendum: tabel butir 11+12 (seq, drain_gap_detected, capture_ms end-to-end, repair-on-read), angka gate aktual, penegasan jalur live tak berubah + tripwire tetap. Status: task, status tiap task, command + exit code, artefak, gate terbuka sisa (butir 13–16).

```bash
git add docs/reports/2026-10-06-per-stream-acceptance.md .runtime/phase-1-ordering/status.json
git commit -m "docs(phase-1): ordering implementation addendum and status"
```

---

## Self-Review

1. **Cakupan spec:** butir 11 → Task 1 (seq JS) + Task 2 (validasi + `drain_gap_detected`) + Task 3 (jam capture end-to-end); butir 12 → Task 4 (record-repair) + Task 5 (bukti nol-tambah-latensi: live merge tak tersentuh, hanya tambah cap atribut). Observabilitas: `drain_gap_detected` + `drain_gaps` di report + tripwire `order_inversion_detected` yang sudah ada. Nol-tambah-latensi agent: tidak ada holdback/watermark di jalur live; repair hanya di baca-record offline.
2. **Pindai placeholder:** tiap step berisi kode/ perintah aktual; helper tes (`_registry`, `_chunk`, `frame`, akses baris journal) dirujuk ke pola helper yang sudah ada di berkas tes masing-masing, bukan TBD.
3. **Konsistensi tipe:** `seq: int` JS→`StreamChunk.seq: int`→`StreamSession.seq: int`→`note_capture(seq: int, ...)`→`frame.capture_seq`; `page_ms: float`→`capture_ms: float`→`TranscriptEvent.capture_ms`→`TranscriptEntry.capture_ms`; `attribute_stream_frame` 5-tuple dipakai konsisten di `join.py:177`; `repaired_order` memakai field yang ada di Task 3.
