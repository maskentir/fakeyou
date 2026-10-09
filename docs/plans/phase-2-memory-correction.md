# Phase 2 Diarization Consumers: Memory Correction Wiring — Implementation Plan

> **Untuk worker agen:** SUB-SKILL WAJIB: subagent-driven-development.
> Kerjakan plan task-per-task. Step memakai sintaks checkbox (`- [ ]`).

## Goal

Menyambung hasil Fase B (sidecar `diarization_correction/v1`) ke konsumen:
ingestion memori Graphiti mengoreksi fakta salah-atribusi, dan debrief
mengutip pembicara yang benar. Tanpa ini Fase B tidak terhubung ke apa pun.

## Success Criteria

- Jendela yang berubah menurut sidecar di-ingest ulang sebagai episode
  koreksi; fakta lama terinvalidasi (`invalid_at` terisi), recall repo
  berhenti menyajikannya (superseded_serve_rate = 0 di eval).
- Debrief memakai `corrected_speaker_uuid` untuk atribusi kutipan bila
  sidecar cocok dan segar; tanpa sidecar hasilnya identik hari ini.
- Eval Lapis 3 berversi hijau; metrik headline terdokumentasi dengan angka.
- Suite penuh + ruff + mypy hijau; tiap task review terpisah bersih.

## Approach

Opsi A hasil spike (re-ingest koreksi, `reference_time` = waktu jalan
koreksi — BUKAN waktu jendela, supaya gate `valid_at` strict lolos;
spike 3/3 stabil). Eval mengukur di lapisan repo (`describe_facts`,
bukan `search` mentah) karena Graphiti tetap mengembalikan edge basi.
Pemetaan jendela→episode disimpan aditif untuk pengukuran dan fallback
Opsi C. Debrief memakai `load_corrected_record` tanpa menyentuh Graphiti.

**Spec:**
[2026-10-09-graphiti-correction.md](../reports/2026-10-09-graphiti-correction.md)
(mekanisme invalidasi + putusan A-bersyarat) dan
[2026-10-09-memory-correction-eval.md](../reports/2026-10-09-memory-correction-eval.md)
(§4-§7 strategi eval + hasil spike §9). Pelaksana membaca keduanya.

## Global Constraints

- Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia
  di `.py`; dokumentasi Bahasa Indonesia.
- Tanpa dependency baru (stdlib + graphiti-core yang sudah ada).
- Tanpa protokol privat Meet; tanpa teks transkrip di jurnal.
- `record.jsonl` tidak pernah ditulis; stale sidecar ditolak, bukan dipakai.
- Episode koreksi memakai `reference_time` waktu koreksi berjalan.
- Tes merah dulu; pytest serial (`-p no:cacheprovider`, tanpa `-n`).
- Tiap task direview reviewer terpisah sebelum commit lokal.

Keputusan pemilik yang mengikat: Opsi A bersyarat (spike §9);
Opsi C fallback bila flaky; shadow Lapis 4 = follow-up eksplisit.

---

### Task 1: Pemetaan jendela→episode + penulis koreksi

**Files:**
- Modify: `src/fakeyou/ingestion_status.py` (kolom aditif
  `episode_uuids: dict[int, str]`, default kosong; status lama tetap load),
  `src/fakeyou/ingestion.py` (simpan UUID per jendela; fungsi baru
  `ingest_corrections`)
- Test: tambah ke `tests/test_ingestion.py`,
  `tests/test_ingestion_status.py` (atau buat bila belum ada)

**Interfaces:**
- Consumes: `load_corrected_record` (Fase B),
  `meeting_windows` (bentuk jendela yang sama dengan ingestion awal),
  fake `make_graphiti` (tambah skrip `get_nodes_and_edges_by_episode`
  bila perlu untuk pengukuran).
- Produces (dipakai Task 3 apa adanya):
  `ingest_corrections(memory, record_path, *, group_id, meeting_id,
  journal=None, faults=None) -> CorrectionResult` dengan
  `CorrectionResult` (frozen dataclass: `changed_windows: int`,
  `episodes: tuple[str, ...]` UUID episode koreksi, `completed: bool`):
  hanya jendela yang `corrected_speaker_uuid`-nya mengubah atribusi
  vs label live yang di-ingest ulang sebagai episode
  `EpisodeType.message` (`source_description` =
  `"Google Meet <id> transcript (corrected)"`,
  `reference_time` = now UTC, saga meeting yang sama,
  `saga_previous_episode_uuid` = episode terakhir); idempoten
  (jalan 2x = 1x, dideteksi dari status + `corrected_at` sidecar).

**Steps:**
- [ ] Tes merah: status lama tanpa kolom baru tetap load; UUID tersimpan
  per jendela setelah ingest.
- [ ] Tes merah: sidecar mengubah 1 dari 3 jendela → tepat 1 episode
  koreksi dengan body penutur terkoreksi + reference_time ≈ now.
- [ ] Tes merah: jalan 2x tanpa sidecar baru → 0 episode tambahan.
- [ ] Tes merah: sidecar stale → `DiarizeError`, nol episode.
- [ ] Implementasi minimal; zelen.
- [ ] Gate: file tes + `ruff check` + `ruff format --check` + `mypy src`.

**Expected:** FAIL (fungsi/kolom belum ada).

Reviewer terpisah memeriksa diff Task 1 saja. Lalu:

### Task 2: Wiring debrief ke record terkoreksi

**Files:**
- Modify: `src/fakeyou/debrief_sources.py`
  (`load_record_snapshot` memakai entri terkoreksi untuk atribusi:
  nama kutipan dari `corrected_speaker_uuid` bila ada, else label live),
  `src/fakeyou/debrief.py` bila perlu meneruskan sinyal koreksi
- Test: tambah ke `tests/test_debrief_sources.py` (atau setara yang ada)

**Interfaces:**
- Consumes: `load_corrected_record` (Fase B) — tanpa sidecar hasilnya
  identik `load_meeting_record`; sidecar stale → fallback record asli +
  fault (jangan gagalkan debrief).
- Produces: snapshot memakai atribusi terkoreksi; perilaku tanpa-sidecar
  bita-identik (dijamin tes).

**Steps:**
- [ ] Tes merah: fixture record + sidecar cocok → kutipan teratribusi ke
  penutur terkoreksi.
- [ ] Tes merah: tanpa sidecar → snapshot identik baseline; sidecar stale
  → fallback + fault tercatat.
- [ ] Implementasi minimal; zelen.
- [ ] Gate: file tes + `ruff check` + `ruff format --check` + `mypy src`.

**Expected:** FAIL (atribusi masih label live).

Reviewer terpisah (orang berbeda dari Task 1). Lalu:

### Task 3: Harness eval Lapis 3 + fixture + gate metrik

**Files:**
- Create: `evals/test_memory_correction_eval.py` (konvensi
  `evals/test_*_eval.py` yang ada),
  `evals/fixtures/memory-correction-v1.json` (3 jendela + koreksi +
  10 query + jawaban emas dari spike §7/§9)
- Modify: docs (runbook/metrik) bila harness butuh prasyarat
  (Neo4j sekali pakai + kunci LLM)

**Interfaces:**
- Consumes: Task 1 (`ingest_corrections`), lapisan recall repo
  (`describe_facts`, BUKAN `search` mentah).
- Produces: harness yang dilaporkan per metrik —
  `invalidation_recall` (edge jendela berubah ber-`invalid_at`),
  `superseded_serve_rate` (jawaban recall menyajikan fakta basi),
  `corrected_answer_accuracy` (pencocokan string deterministik dulu;
  judge hanya bila perlu, terkalibrasi), `over_invalidation`,
  `entity_merge_rate`; gate: invalidation 1,0 + superseded 0 pada
  fixture v1; harness di-skip anggun tanpa Neo4j/LLM (bukan xfail).

**Steps:**
- [ ] Fixture v1 dari artefak spike (disamarkan, deterministik).
- [ ] Tes merah: harness gagal bila recall menyajikan fakta basi.
- [ ] Implementasi harness; hijau 3 run berurutan (stabilitas LLM).
- [ ] Gate: harness hijau + `ruff check` + `ruff format --check`.
- [ ] Catat angka + biaya/run di laporan adendum.

**Expected:** FAIL (harness belum ada).

Reviewer terpisah (orang berbeda dari Task 1-2). Lalu:

Follow-up eksplisit di luar plan: shadow Lapis 4, trigger otomatis,
fallback Opsi C bila flaky, kualitas ekstraksi satu-ujaran.

## Validation Plan

- Per task: file tes task + `ruff check` + `ruff format --check` +
  `mypy src` pada file tersentuh (perintah di tiap task).
- Akhir: suite penuh
  `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false .venv/bin/pytest tests/
  -q -p no:cacheprovider` + eval Lapis 3 hijau 3x + final review cabang.
- Validasi tertinggi-risiko: eval Lapis 3 (perilaku LLM nondeterministik;
  fixture menegaskan properti, bukan count — temuan spike).

## Risks / Open Questions

- Flakiness LLM di dedup/kontradiksi pada fixture produksi yang lebih
  besar → mitigasi: Opsi C fallback siap di Task 1 (pemetaan sudah ada).
- `reference_time` koreksi = now menggeser `valid_at` fakta (benar secara
  gate, agak salah semantik waktu ujaran) → diterima; didokumentasikan.
- Open questions: None (spike menjawab mekanisme; biaya LLM eval kecil).

## Self-Review

- [ ] Tiap kebutuhan spec (koreksi ingestion, debrief, eval) punya task.
- [ ] Tanpa placeholder: nama fungsi, skema, metrik eksplisit.
- [ ] Konsistensi tipe: `CorrectionResult`, status aditif, fixture v1 —
  cocok antar-task.
