# Phase 2 Item 18 Fase A: Persistensi Audio Sesi — Implementation Plan

> **Untuk worker agen:** SUB-SKILL WAJIB: subagent-driven-development.
> Kerjakan plan task-per-task. Step memakai sintaks checkbox (`- [ ]`).

**Goal:** Simpan audio per-slot tiap sesi sebagai WAV + sidecar
jangkar jam, dengan retensi, sebagai prasyarat diarisasi offline.

**Architecture:** Writer sinkron di `StreamRegistry.feed`
(per-slot WAV 16 kHz mono 16-bit + isi-hilang + sidecar
`slots.json` berisi jangkar page_ms-vs-monotonic),
di balik flag default mati; purge CLI terpisah;
mode campuran TIDAK dicakup Fase A (legacy —
catat follow-up).

**Tech Stack:** Python stdlib `wave`/`struct` saja (tanpa dep baru),
pytest serial, ruff, mypy.

**Spec:** [2026-10-07-fase-2-research.md](../reports/2026-10-07-fase-2-research.md)
(bagian 2 + Keputusan: audio campuran boleh disimpan +
retensi). Pelaksana membaca bagian itu plus plan ini.

## Global Constraints

- Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia
  di `.py`; dokumentasi Bahasa Indonesia.
- Tanpa dependency baru (stdlib saja).
- Tanpa protokol privat Meet; tanpa teks transkrip di jurnal/sidecar.
- Flag default mati; flag-mati = perilaku hari ini identik
  (nol file audio, nol event baru).
- Event jurnal baru wajib berakhiran `EventVerb`:
  `audio_recording_started`, `audio_recording_finished`.
- Tes merah dulu; pytest serial (`-p no:cacheprovider`, tanpa `-n`).
- Tiap task direview reviewer terpisah sebelum commit lokal.

Keputusan pemilik yang mengikat: persistensi disetujui;
retensi 30 hari default (bisa setel, keputusan controller
yang dicatat — pemilik menyetujui konsep retensi tanpa angka);
per-slot WAV (bukan satu mix) — ruling controller: byte
identik, alignment sekuens eksak offline, nol DSP live.

---

### Task 1: Writer + settings + event

**Files:**
- Create: `src/fakeyou/audio_persist.py`
- Modify: `src/fakeyou/settings.py` (Section baru)
- Test: `tests/test_audio_persist.py` (baru)

**Interfaces:**
- Consumes: `StreamChunk` (slot/seq/page_ms/pcm),
  `Journal`, `CAPTURE_SAMPLE_RATE`.
- Produces (dipakai Task 2 apa adanya):
  `class AudioSettings(Section)` dengan
  `persist_enabled: bool = False`,
  `retention_days: int = Field(default=30, gt=0)`;
  `class SlotAudioWriter` dengan
  `__init__(self, directory: Path, *, monotonic: Callable[[], float] = time.monotonic, journal: Journal | None = None)`,
  `write_chunk(self, slot: int, seq: int, page_ms: float, pcm: bytes) -> None`,
  `close(self) -> AudioRecordingSummary`;
  `@dataclass(frozen=True, slots=True) class AudioRecordingSummary`
  (`slots: tuple[int, ...]`, `seconds_total: float`,
  `bytes_total: int`, `gaps_filled: int`, `chunks: int`).
  File: `<directory>/slot-<n>.wav` (16 kHz mono 16-bit,
  ditulis bertahap, header final saat `close`),
  `<directory>/slots.json` (ditulis saat `close`):
  `{"schema": "slot_audio/v1", "slots": {str(slot):
  {"seq_start": int, "seq_end": int, "frames": int,
  "gaps_filled": int}}, "anchors": [{"slot": int,
  "seq": int, "page_ms": float, "mono": float}]}`.
  Gap sekuens (seq > expected): isi byte-nol sepanjang
  selisih × durasi-chunk (durasi dari `len(pcm)` aktual,
  BUKAN konstanta — chunk terakhir bisa pendek);
  seq duplikat/lama (≤ expected terakhir): buang diam-diam
  (resync drain, sudah dijurnalkan `drain_gap_detected`
  oleh registry). Jangkar dicatat per chunk (beban kecil).
  `close()` idempotent; menjurnalkan SATU
  `audio_recording_finished` (kunci: `slots` list,
  `seconds_total` round 3, `bytes_total`, `gaps_filled`,
  `chunks`). Konstruktor menjurnalkan
  `audio_recording_started` (kunci: `directory` basename
  SAJA — tanpa path absolut — agar jurnal portabel).

- [ ] **Step 1: tulis tes merah writer**

Kasus (`tmp_path`, journal fake/nyata? pakai `Journal`
nyata ke file tmp bila polanya ada di repo — bila tidak,
fake minimal dengan `record()`; JANGAN mock antar-unit
sendiri): (a) 3 chunk sekuensial slot 0 → WAV terbaca
kembali via stdlib `wave` (16 kHz/mono/16-bit, frames
tepat); (b) lompat seq 5→8 → selisih diisi nol
(verifikasi byte region gap == 0) + `gaps_filled == 1`;
(c) chunk duplikat/lama dibuang (frames tak berubah);
(d) dua slot → dua WAV + `slots.json` memuat keduanya
dengan jangkar per chunk; (e) `close()` ganda aman +
SATU event finished dengan angka eksak;
(f) event started membawa basename saja (tanpa `/`).

- [ ] **Step 2: jalankan tes, pastikan gagal**

Run:
`.venv/bin/python -m pytest tests/test_audio_persist.py -q -p no:cacheprovider`
Expected: FAIL (modul belum ada).

- [ ] **Step 3: implementasi minimal**

Tulis settings + writer. WAV bertahap: kumpulkan PCM
per slot di memori lalu tulis SEKALIGUS saat `close`
(sesi ≤2 jam × 3 slot × 115 MB/jam ≈ 700 MB — TERLALU
BESAR untuk memori; JANGAN buffering penuh).
Implementasi benar: buka handle `wave` per slot saat
chunk pertama (lazy), `writeframes` per chunk (+ nol
untuk gap), `close()` semua handle + tulis sidecar.
Batasi file terbuka = jumlah slot (≤3, aman).

- [ ] **Step 4: jalankan tes, pastikan lulus**

Run: perintah Step 2. Expected: PASS.

- [ ] **Step 5: gate task**

Run: file tes baru penuh, `ruff check` + `ruff format --check`,
`mypy` pada `src/fakeyou/audio_persist.py src/fakeyou/settings.py`.
Expected: hijau.

- [ ] **Step 6: review + commit**

Reviewer terpisah memeriksa diff Task 1 saja. Lalu:

```bash
git add src/fakeyou/audio_persist.py src/fakeyou/settings.py tests/test_audio_persist.py
git commit -m "feat(diarize): slot WAV writer and settings"
```

### Task 2: Wiring registry + purge CLI + docs

**Files:**
- Modify: `src/fakeyou/stream_capture.py` (hook recorder),
  `src/fakeyou/cli/join.py` ATAU `src/fakeyou/assembly.py`
  ( SATU titik wiring flag → direktori
  `<record_dir>/<meeting_id>/audio/`; pilih yang sudah
  memegang settings + record_dir + lifecycle close,
  catat pilihan di laporan)
- Create: `src/fakeyou/cli/audio.py`
  (`fakeyou audio purge-expired [--root] [--days]`)
- Modify: `src/fakeyou/cli/main.py` (registrasi)
- Test: `tests/test_audio_persist.py` (tambah kasus wiring),
  `tests/test_cli_audio.py` (baru)

**Interfaces:**
- Consumes: Task 1 (`SlotAudioWriter`), `StreamRegistry.feed`,
  `PathSettings.record_dir`, `AudioSettings`.
- Produces: `StreamRegistry(..., recorder: SlotAudioWriter | None = None)`
  — `feed()` memanggil `recorder.write_chunk(...)`
  SETELAH update state sesi (dalam try/except:
  error tulis → `record_consumer_fault` source audio,
  detail `audio_persist`, sesi tetap jalan);
  wiring flag: writer dibuat + `close()` di akhir sesi
  HANYA bila `persist_enabled` (default mati = args tak
  berubah, nol file, nol event — assert ini di tes);
  `fakeyou audio purge-expired` menghapus direktori
  `audio/` meeting yang `slots.json`-mtime (fallback:
  mtime direktori) lebih tua dari `retention_days`
  (default dari settings, `--days` override),
  mencetak JSON `{"removed": [...basenames...],
  "kept": N, "freed_bytes": B}` ke stdout, exit 0
  selalu bila root valid (root tak ada → exit 2 +
  pesan menyebut path).

- [ ] **Step 1: tulis tes merah wiring + purge**

Kasus wiring (registry nyata + writer ke `tmp_path`,
tanpa browser): (a) flag on → feed 3 chunk (satu gap)
menghasilkan WAV + sidecar + event started/finished
di journal; (b) flag off → feed sama menghasilkan
NOL file audio + NOL event audio (perilaku identik);
(c) writer error (direktori read-only) → fault
tercatat + sesi/feed tetap sukses. Kasus purge (CLI
runner): (d) direktori tua dihapus + JSON tepat
(removed/kept/freed_bytes); (e) direktori muda
dipertahankan; (f) root tak ada → exit 2.

- [ ] **Step 2: jalankan tes, pastikan gagal**

Run: dua file tes. Expected: FAIL (hook + CLI belum ada).

- [ ] **Step 3: implementasi minimal**

Tambah parameter `recorder` + try/except di `feed`;
titik wiring + lifecycle; tulis `cli/audio.py` +
registrasi `main.py` (cerminkan pola `memory_*`).

- [ ] **Step 4: jalankan tes, pastikan lulus**

Run: perintah Step 2. Expected: PASS.

- [ ] **Step 5: gate task**

Run: dua file tes penuh + `tests/test_documented_commands.py`,
`ruff check` + `ruff format --check`, `mypy` pada semua
`src` yang diubah. Expected: hijau.

- [ ] **Step 6: review + commit**

Reviewer terpisah (orang berbeda dari Task 1). Lalu:

```bash
git add src/fakeyou/stream_capture.py <titik-wiring> src/fakeyou/cli/audio.py src/fakeyou/cli/main.py tests/test_audio_persist.py tests/test_cli_audio.py
git commit -m "feat(diarize): registry wiring and purge CLI"
```

### Task 3: Live smoke twin-only + analisis offset (bukan kode)

Setelah Task 1–2 merge: sesi twin-only ±10 menit dengan
`persist_enabled` on. Verifikasi: WAV per slot terbaca
(durasi ≈ durasi sesi aktif slot), `slots.json` valid,
drift jam page_ms-vs-mono dari jangkar (lapor ms/menit),
matematika ukuran (~115 MB/jam/slot-aktif),
`audio_recording_started/finished` di jurnal, purge
kering (dry-run logika via `--days` besar = kept semua).
Butuh link Meet dari pemilik saat siap. Hasil + angka
offset ditulis sebagai adendum laporan acceptance.
Follow-up eksplisit di luar Fase A: mode campuran,
job diarisasi + sidecar koreksi (Fase B).

## Self-Review

- Cakupan: persistensi + retensi + offset (prasyarat
  riset §2) → Task 1–3; engine diarization + sidecar
  koreksi + reader eksplisit Fase B. Tak ada gap.
- Placeholder: tak ada; signature, skema sidecar, nama
  event, perintah CLI, dan output JSON eksplisit.
  Satu titik diskresi (wiring) dibatasi + wajib dicatat.
- Konsistensi: `SlotAudioWriter`/`AudioRecordingSummary`
  Task 1 dipakai verbatim Task 2; nama event identik;
  direktori `<record_dir>/<id>/audio/` konsisten.
