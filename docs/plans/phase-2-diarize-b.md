# Phase 2 Item 18 Fase B: Correction Pass Diarization — Implementation Plan

> **Untuk worker agen:** SUB-SKILL WAJIB: subagent-driven-development.
> Kerjakan plan task-per-task. Step memakai sintaks checkbox (`- [ ]`).

**Goal:** Job pasca-sesi yang mengoreksi atribusi speaker ("siapa bilang
apa") memakai endpoint file Muse Voice Transcribe mode DIARIZATION,
menulis sidecar koreksi, dan menyediakannya lewat satu choke point baca.
`record.jsonl` asli tetap identik bita.

**Architecture:** CLI manual `fakeyou diarize run --meeting ID` (container
terpisah, lock `diarization` per sesi): campur WAV per-slot Fase A secara
offline (stdlib, tanpa DSP live) → potong ≤10 menit/32MB → POST
`/v1/asr/transcribe` → gabung turn pendek palsu → tulis
`diarization.rttm` + `diarization.json` skema `diarization_correction/v1`
(stale-check `record_sha256`, UUID koreksi namespace uuid5 terpisah) →
baca via `load_corrected_record`.

**Tech Stack:** Python stdlib saja (`wave`/`struct`/`urllib`), pytest
serial, ruff, mypy.

**Spec:** [2026-10-07-fase-2-research.md](../reports/2026-10-07-fase-2-research.md)
(bagian 2 + Keputusan: audio boleh disimpan + retensi) dan
[2026-10-08-muse-voice-transcribe.md](../reports/2026-10-08-muse-voice-transcribe.md)
(§6 hasil spike endpoint file). Pelaksana membaca keduanya plus plan ini.

## Global Constraints

- Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia
  di `.py`; dokumentasi Bahasa Indonesia.
- Tanpa dependency baru (stdlib saja).
- Tanpa protokol privat Meet.
- Tanpa teks transkrip di jurnal maupun sidecar (sidecar hanya waktu,
  label, UUID koreksi).
- `record.jsonl` tidak pernah ditulis job ini (stale-check menolak bila
  berubah).
- Event jurnal baru: tidak ada (job offline, tanpa jurnal).
- Tes merah dulu; pytest serial (`-p no:cacheprovider`, tanpa `-n`).
- Tiap task direview reviewer terpisah sebelum commit lokal.

Keputusan pemilik yang mengikat: engine Opsi M (endpoint file Muse, hasil
spike purity 1,000); trigger manual via CLI (otomatis = follow-up);
retensi audio ikut Fase A; kunci API pakai ulang `FAKEYOU_STT__API_KEY`.

---

### Task 1: Mixer + chunker offline

**Files:**
- Create: `src/fakeyou/diarize_mix.py`
- Test: `tests/test_diarize_mix.py` (baru)

**Interfaces:**
- Consumes: WAV per-slot Fase A (mono 16-bit 16 kHz, `seq_start` sama =
  selaras sampel) dari direktori audio sesi.
- Produces (dipakai Task 3 apa adanya):
  `mix_session_slots(audio_dir: Path) -> bytes` (WAV mono 16-bit 16 kHz,
  jumlah sampel per slot, clamp int16, panjang = slot terpanjang);
  `split_chunks(frames: bytes, frames_per_second: int) -> list[bytes]`
  (tiap chunk ≤10 menit DAN ≤32MB termasuk header WAV 44 byte).

**Steps:**
- [ ] Tes merah: campur dua WAV sintetis (nada murni berbeda) → jumlah
  benar + clamp; panjang ikut slot terpanjang; slot pendek diisi nol.
- [ ] Tes merah: chunk 25 menit → 3 chunk (10+10+5); chunk ≤32MB.
- [ ] Implementasi minimal; zelen.
- [ ] Gate: file tes + `ruff check` + `ruff format --check` + `mypy src`.

**Expected:** FAIL (modul belum ada).

Reviewer terpisah memeriksa diff Task 1 saja. Lalu:

### Task 2: Klien file + heuristik gabung

**Files:**
- Create: `src/fakeyou/diarize_client.py`
- Test: `tests/test_diarize_client.py` (baru)

**Interfaces:**
- Consumes: chunk WAV Task 1, `api_key: str`, `base_url: str`
  (default `https://api.meta.ai/v1`).
- Produces (dipakai Task 3 apa adanya):
  `DiarizedTurn` (frozen dataclass: `start_ms: int`, `end_ms: int`,
  `speaker: str`); `transcribe_chunk(chunk: bytes, *, api_key, base_url,
  opener) -> list[DiarizedTurn]` (multipart part `request` JSON
  `{"model": "muse-voice-transcribe-1.0", "mode": "DIARIZATION",
  "audioEncoding": "WAV"}` + part `audio`; `opener` injeksi HTTP,
  default `urllib.request.urlopen`; HTTPError → `DiarizeError`);
  `merge_short_turns(turns, *, max_ms: int = 2000, gap_ms: int = 1000)`
  (turn < max_ms yang berjarak < gap_ms ke tetangga digabung ke tetangga
  terdekat, label ikut tetangga; iteratif sampai stabil).

**Steps:**
- [ ] Tes merah: respons fixture 3 giliran → parse tepat (tanpa teks
  transkrip di `DiarizedTurn`).
- [ ] Tes merah: HTTPError → `DiarizeError` (opener fake, tanpa jaringan).
- [ ] Tes merah: ekor palsu 1,2 dtk diapit speaker sama → gabung; turn
  pendek terisolasi (>gap) → tetap.
- [ ] Implementasi minimal; zelen.
- [ ] Gate: file tes + `ruff check` + `ruff format --check` + `mypy src`.

**Expected:** FAIL (modul belum ada).

Reviewer terpisah (orang berbeda dari Task 1). Lalu:

### Task 3: CLI run + sidecar + lock

**Files:**
- Create: `src/fakeyou/diarize_correction.py` (skema + tulis sidecar),
  `src/fakeyou/cli/diarize.py` (handler CLI)
- Modify: `src/fakeyou/cli/main.py` (registrasi `diarize run`)
- Test: `tests/test_diarize_correction.py`,
  `tests/test_cli_diarize.py` (baru)

**Interfaces:**
- Consumes: Task 1 (`mix_session_slots`, `split_chunks`), Task 2
  (`transcribe_chunk`, `merge_short_turns`), `Settings`
  (`stt.api_key`, path audio sesi), `file_lease` (kunci `diarization`).
- Produces:
  `write_correction_bundle(audio_dir, record_path, turns, *, clock) ->
  CorrectionBundle` (tulis `diarization.rttm` + `diarization.json`
  skema `diarization_correction/v1`: `schema`, `record_sha256` (sha256
  bita `record.jsonl`), `turns[]` berisi `start_ms/end_ms/speaker/
  corrected_uuid` dengan `corrected_uuid =
  uuid5(NAMESPACE_URL, "diarization-correction/v1:" + speaker + ":" +
  str(start_ms))`); handler `diarize_run(meeting_id, *, settings,
  opener=None) -> int` (exit 0 sukses; kunci per sesi; tanpa kunci API →
  `CliError` tanpa membocorkan secret).

**Steps:**
- [ ] Tes merah: bundle sintetis → dua file ada; `record.jsonl` bita
  identik sebelum/sesudah; UUID deterministik.
- [ ] Tes merah: CLI tanpa kunci API → `CliError` (pesan tanpa secret).
- [ ] Tes merah: dua run bersamaan → yang kedua menolak lewat lock
  (fake lease bila perlu).
- [ ] Implementasi minimal; zelen.
- [ ] Gate: file tes + `ruff check` + `ruff format --check` + `mypy src`.

**Expected:** FAIL (modul belum ada).

Reviewer terpisah (orang berbeda dari Task 1-2). Lalu:

### Task 4: Choke point baca + show + runbook

**Files:**
- Modify: `src/fakeyou/diarize_correction.py` (tambah baca),
  `src/fakeyou/record.py` (field opsional `corrected_speaker_uuid` di
  `TranscriptEvent`, default None, tanpa ubah serialisasi lama),
  `src/fakeyou/cli/diarize.py` (subcommand `show`),
  `src/fakeyou/cli/main.py` (registrasi), `docs/runbooks/` (runbook baru
  atau adendum worker-operations)
- Test: tambah ke `tests/test_diarize_correction.py`,
  `tests/test_cli_diarize.py`

**Interfaces:**
- Consumes: sidecar Task 3, `load_meeting_record`.
- Produces:
  `load_corrected_record(record_path, *, tolerance_ms: int = 2000) ->
  list[RecordEntry]` (tanpa sidecar → record asli; sha cocok →
  tiap entri transkrip dapat `corrected_speaker_uuid` dari turn yang
  memuat titik tengah `capture_ms` entri; turn dipakai untuk offset
  WAV-relatif + `session_start_page_ms` dari `slots.json`; di luar
  toleransi → None; sha beda → `DiarizeError` stale).

**Steps:**
- [ ] Tes merah: fixture record + sidecar cocok → UUID terisi tepat;
  tanpa sidecar → identik asli; sha beda → `DiarizeError`.
- [ ] Tes merah: `diarize show` mencetak ringkasan (n turn, cakupan).
- [ ] Runbook Bahasa Indonesia: kapan run, biaya (±$0,18/jam audio),
  cara baca koreksi, retensi.
- [ ] Implementasi minimal; zelen.
- [ ] Gate: file tes + `ruff check` + `ruff format --check` + `mypy src`.

**Expected:** FAIL (fungsi belum ada).

Reviewer terpisah (orang berbeda dari Task 1-3). Lalu:

### Task 5: Validasi offline sesi rekaman (bukan kode)

Setelah Task 1-4 merge: jalankan `fakeyou diarize run` pada satu sesi v19
berisi dua manusia (butuh `FAKEYOU_STT__API_KEY` di `.env`, biaya
±$0,05-0,10):

- [ ] `diarization.rttm` + `diarization.json` tertulis valid.
- [ ] `record.jsonl` bita-identik (sha sebelum/sesudah sama).
- [ ] `diarize show` melapor; cakupan turn vs entri dicatat.
- [ ] Adendum laporan: angka validasi + keputusan adopsi konsumen.

Follow-up eksplisit di luar Fase B: wiring konsumen (debrief/ingestion),
trigger otomatis pasca-sesi, engine pyannote bila butuh separasi overlap.

## Self-Review

- [ ] Konsistensi antar-task: Task 1→3 (mix/chunk verbatim), Task 2→3
  (turn/merge verbatim), Task 3→4 (sidecar verbatim). Tak ada gap.
- [ ] Setiap modul/fungsi publik diuji merah-dulu.
- [ ] Tanpa teks transkrip di sidecar/jurnal (skema hanya waktu+label+UUID).
- [ ] Tanpa dependency baru; tanpa tulisan Indonesia di `.py`.
