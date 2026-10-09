# Rencana Tindak Lanjut Fase 2

Status: draf implementasi. Bahasa dokumen: Indonesia. Kode Python tetap Inggris tanpa komentar/docstring.

Sumber: 6 laporan riset fase 2 (idempotency per-jendela, shadow Lapis 4, ekstraksi
isolated-utterance, auto-trigger, pemetaan UUID-label, pengerasan gerbang vacuus).
Setiap tugas di bawah merangkum satu laporan menjadi desain kompak yang siap
dikerjakan dengan red-test terlebih dahulu.

## Kendala Global (berlaku untuk semua tugas)

- Python Inggris, tanpa komentar dan tanpa docstring pada kode baru/ubah.
- Dokumen dan runbook berbahasa Indonesia.
- Tanpa dependensi baru (tidak boleh menambah `pyproject.toml` / `uv.lock`).
- Tanpa push/merge; kerja lokal, satu reviewer terpisah per tugas.
- Ledger SDD: `.superpowers/sdd/phase-2-followups/progress.md` wajib dimutakhirkan
  per tugas (status, bukti perintah, keputusan).
- Verifikasi baku setiap tugas (jalankan semuanya, bukan salah satu):

```bash
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false uv run pytest tests/<target> -x -q
ruff check src tests evals
ruff format --check src tests evals
mypy src
```

Urutan kerja yang disarankan: 1 -> 4 -> 3 -> 5 -> 6 -> 2 (idempotency dulu
sebagai fondasi retry, lalu auto-trigger yang bergantung padanya).

---

## Tugas 1 — Idempotency Per-Jendela (`ingest_corrections`)

### Tujuan

Retry koreksi memori yang gagal di tengah jalan tidak boleh menimbulkan episode
duplikat dan tidak boleh kehilangan progres parsial; ganti `corrected_at`
me-reset status secara bersih.

### Berkas dan fungsi yang disentuh

- `src/fakeyou/ingestion_status.py`: `IngestionStatus`, `write_status`,
  `read_status` (tambah field `correction_windows: dict[int, str]` berisi
  pemetaan jendela -> episode koreksi, dan `correction_run: str | None`;
  default kosong agar JSON lama tetap lolos validasi; `corrected_at` tetap
  penanda selesai-penuh).
- `src/fakeyou/ingestion.py`: `ingest_corrections`, `correction_episode_name`,
  `_resume_point` (logika retry: jika `existing.corrected_at == corrected_at`
  langsung kembalikan selesai; jika `correction_run != corrected_at` reset peta;
  lewati `changed` yang sudah ada di peta; `previous_uuid` tetap dari
  `meeting_episodes` dengan `last_n=1`; tulis atomik per jendela sukses via
  `write_status` merge; `corrected_at` + `correction_episodes` hanya ditulis
  saat semua jendela selesai; anti-orphan: sebelum tulis, pindai
  `meeting_episodes` model batch ganda ala `_resume_point` dan lewati nama
  `correction_episode_name` yang sudah ada).
- `tests/test_ingestion.py` (atau berkas tes koreksi yang ada): tambah red test.

### Tes merah yang ditulis dulu

1. Gagal-di-tengah lalu retry: simulasikan kegagalan setelah jendela 1 dari 3
   sukses, retry harus menyelesaikan tanpa duplikat episode.
2. Peta parsial bertahan: setelah kegagalan, `correction_windows` memuat
   jendela yang sudah sukses dan retry melewatinya.
3. Ganti `corrected_at` me-reset peta (`correction_run` baru mengosongkan map).
4. Gagal tulis status: `write_status` melempar -> hasil `completed is False`
   dan tidak ada klaim selesai palsu.

### Kriteria terima (ambang metrik)

- Episode duplikat setelah retry gagal-di-tengah: `== 0`.
- Jendela sukses parsial yang ditulis ulang saat retry: `== 0`.
- `corrected_at` hanya terisi saat semua jendela selesai: 100% kasus uji.
- JSON status lama tanpa field baru tetap terbaca: 100% (uji backward-compat).

### Perintah verifikasi

```bash
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false uv run pytest tests/test_ingestion.py -x -q
ruff check src tests evals
ruff format --check src tests evals
mypy src
```

### Di luar lingkup

- Perubahan skema `meeting_episodes` atau format sidecar diarizasi.
- Retry lintas proses/paralel (tetap satu penulis per meeting via lock).
- Migrasi massal JSON status lama (cukup default kosong, tanpa skrip migrasi).

---

## Tugas 2 — Shadow Lapis 4 (eval koreksi, offline-capable)

### Tujuan

Mengirim evaluasi perbandingan prod-vs-shadow sekarang dengan kemampuan offline,
tanpa menunggu sampler produksi, scheduler, atau LLM-judge yang belum ada.

### Berkas dan fungsi yang disentuh

- `evals/test_shadow_correction_eval.py` (baru): red-test dulu; pakai ulang
  fixture v1 + helper Lapis 3 dari `evals/test_memory_correction_eval.py`:
  `answer_queries` (group-scoped) dan fungsi metrik murni; dual-run fixture
  grup A prod-sim tanpa koreksi vs grup B shadow dengan `ingest_corrections`
  Opsi C; artefak diff-report JSON `{query_id, prod, shadow, token-match,
  stale_served}` + agregat `superseded` per sisi.
- `src/fakeyou/ingestion.py`: `ingest_corrections` (dipakai apa adanya).
- `src/fakeyou/memory.py`: `describe_facts` (isolasi baca via filter grup).
- Runbook Indonesia baru di `docs/` (nama: `docs/shadow-correction-runbook.md`):
  cara jalan, cara baca diff-report, gate sementara.
- Konvensi grup `<prod>-shadow-<ts>` harus lolos validasi `group_id`.

### Tes merah yang ditulis dulu

1. Unit diff murni (default CI, non-live): dua jawaban fixture menghasilkan
   `token-match` dan `stale_served` yang benar serta agregat `superseded`
   per sisi.
2. Dual-run fixture: grup B shadow memiliki `superseded_shadow == 0` sementara
   grup A prod-sim `superseded_prod >= 1`.
3. Satu tes live graceful-skip: Neo4j sekali pakai, `group_id` unik,
   skip rapi saat DB tidak tersedia.

### Kriteria terima (ambang metrik)

- Gate sementara: shadow menang bila `superseded_shadow == 0 < superseded_prod`
  pada fixture: lolos 100%.
- Unit diff murni berjalan di CI default tanpa live/DB: 100% pass.
- Tes live tanpa DB: skip (bukan gagal), dengan pesan skip eksplisit.
- Tidak ada recall shadow bocor ke prod pada fixture: `stale_served` sisi prod
  tidak berubah oleh run shadow.

### Perintah verifikasi

```bash
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false uv run pytest evals/test_shadow_correction_eval.py -x -q
ruff check src tests evals
ruff format --check src tests evals
mypy src
```

### Di luar lingkup (eksplisit TERBLOKIR, jangan dikerjakan di sini)

- Sampler query produksi (sumber query sampel prod + akses record prod tidak ada).
- Scheduler job dan store metrik gerbang promosi 2-minggu.
- Blind LLM-judge terkalibrasi + alur adjudikasi manusia.
- Kebijakan tulis shadow ke Neo4j produksi (DB sama vs terpisah) + isolasi saga
  `meeting_saga` lintas grup.
- Metering biaya (Graphiti tidak mengekspos token; shadow menggandakan beban).
- Bukti non-kebocoran recall shadow ke prod di DB bersama lintas grup.

---

## Tugas 3 — Ekstraksi Isolated-Utterance (merge-floor + hint)

### Tujuan

Jendela 1-utterance yang terisolasi tidak lagi hilang saat ekstraksi; seluruh
perubahan hanya lewat pemanggilan repo ke graphiti-core, tanpa API privat.

### Berkas dan fungsi yang disentuh

- `src/fakeyou/ingestion.py`: `_pack_windows` (post-pass merge: gabungkan
  jendela di bawah floor, minimal 2 utterance atau 120 karakter, ke tetangga;
  jaga urutan saga dan `reference_time` utterance pertama).
- `src/fakeyou/memory.py`: `remember_meeting_fact`, `_evidence_kind`
  (tambah satu kalimat ekstraksi untuk fakta satu baris dengan subjek speaker;
  jaga `source_description` berakhiran `transcript` agar `_evidence_kind`
  tetap `meeting_transcript`; fakta pendek di-buffer sampai 2 fakta atau flush).
- `tests/`: red test deterministik via `make_graphiti` + fixture golden
  `episode_body` yang menegaskan properti (bukan jumlah edge).

### Tes merah yang ditulis dulu

1. Satu utterance tertinggal di akhir menempel ke jendela sebelumnya.
2. Seluruh meeting satu utterance tetap ter-ingest tepat satu kali.
3. `remember_meeting_fact` pendek di-buffer sampai 2 fakta atau flush eksplisit.
4. Golden `episode_body`: properti terpenuhi (subjek speaker ada, urutan saga
   benar, `reference_time` dari utterance pertama).

### Kriteria terima (ambang metrik)

- Fixture lone-trailing: utterance terisolasi terikat ke tetangga 100%.
- Fixture single-utterance: tepat 1 episode ter-ingest, `== 1`.
- Fakta pendek tanpa flush: 0 episode sampai buffer penuh/flush.
- Uji unit deterministik (fake, tanpa LLM): 100% pass, 0 flake dalam 3 run.

### Perintah verifikasi

```bash
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false uv run pytest tests/test_ingestion.py -x -q
ruff check src tests evals
ruff format --check src tests evals
mypy src
```

### Di luar lingkup

- Perubahan ambang internal graphiti-core (pustaka pihak ketiga, sumber tidak
  ada di repo).
- Evaluasi LLM live per-provider (hanya nightly eval berversi, bukan CI).
- Klaim presisi temporal untuk utterance berjarak lebar yang di-merge
  (ditandai unresolved, perlu riset lanjutan).

---

## Tugas 4 — Auto-Trigger Diarizasi saat Ingest

### Tujuan

Ingest otomatis menjalankan diarizasi best-effort sebelum ingest transkrip live,
dengan override eksplisit dan kegagalan STT yang tidak menggagalkan ingest.

### Berkas dan fungsi yang disentuh

- `src/fakeyou/cli/memory.py`: `ingest_recorded_meeting` (hook di paling atas,
  SEBELUM blok ingest `record_lock`; panggil `diarize_run` best-effort lalu
  ingest seperti biasa; `_finish_ingest` tetap menjaga `corrected_at`).
- `src/fakeyou/cli/diarize.py`: `diarize_run` (mengambil lock diarizasi sendiri;
  menaruh hook di luar lock ingest menghindari clash lock bersarang).
- `src/fakeyou/cli/memory.py` / definisi flag CLI: tambah flag `--no-correct`;
  pertahankan alur manual `diarize run/show`.
- `src/fakeyou/provider_faults.py`: `classify_exception` + jurnal event untuk
  mencatat fault STT (tangkap `CliError`, lanjutkan dengan transkrip live,
  exit code tidak terpengaruh).
- `src/fakeyou/worker_backend.py` (jalur worker yang spawn `memory-ingest`):
  pastikan ikut tercakup tanpa perubahan perilaku join (join tetap cepat).

### Tes merah yang ditulis dulu

1. Auto-run menulis sidecar SEBELUM ingest berjalan.
2. `--no-correct` melewatkan diarizasi sepenuhnya.
3. Kegagalan STT tetap meng-ingest transkrip live (exit code sukses, fault
   tercatat via `classify_exception` + jurnal).
4. Rerun idempoten: 0 episode koreksi baru saat `corrected_at` cocok
   (konsisten dengan Tugas 1; bundle overwrite deterministik via uuid5 di
   `diarize_correction.py` dan skip di `ingestion.py:ingest_corrections`).

### Kriteria terima (ambang metrik)

- Sidecar tertulis sebelum ingest pada 100% run normal.
- Skip diam-diam saat tidak ada kunci STT atau tidak ada audio slot: 100%,
  tanpa error.
- Kegagalan STT: ingest live tetap selesai, exit code `== 0`, 1 fault tercatat.
- Rerun dengan `corrected_at` sama: episode koreksi baru `== 0`.

### Perintah verifikasi

```bash
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false uv run pytest tests/test_cli_memory.py -x -q
ruff check src tests evals
ruff format --check src tests evals
mypy src
```

### Di luar lingkup

- Perubahan kecepatan/latensi path join (hook hanya di path ingest).
- Retry otomatis STT (cukup sekali best-effort + fault tercatat).
- Penghapusan alur manual `diarize run/show`.

---

## Tugas 5 — Pemetaan UUID-Label Koreksi

### Tujuan

UUID koreksi yang anonim tetap dapat dibaca manusia di debrief/ingest lewat
pemetaan sekali-ikat ke nama tampilan, tanpa mengubah `participant_id` mentah.

### Berkas dan fungsi yang disentuh

- Builder baru `correction_labels.json`: memetakan `corrected_uuid -> nama
  tampilan` dari bukti roster/jurnal yang tumpang-tindih dengan tiap jendela
  turn sidecar; aturan bind-once, nama duplikat -> null, rejoin -> uuid baru.
- `src/fakeyou/debrief.py` + `src/fakeyou/cli/debrief.py` (render debrief):
  tampilkan `Speaker <huruf>, shown as <nama>, unverified`, atau
  `Speaker <huruf>, unverified` saat null; `participant_id` tetap uuid mentah.
- `src/fakeyou/ingestion.py`: `ingest_corrections` (baca label saat ingest bila
  tersedia, tanpa gagal saat berkas absen).
- `tests/`: red test bind-once, duplicate-null, rejoin-new-label, render
  suffix, fixture anonim hanya Host/Guest/Twin.

### Tes merah yang ditulis dulu

1. Bind-once: uuid yang sudah terikat tidak berubah saat bukti baru datang.
2. Duplicate-null: dua uuid mengklaim nama sama -> keduanya null.
3. Rejoin-new-label: uuid baru setelah rejoin mendapat label baru.
4. Render suffix: string debrief memuat pola `Speaker <huruf>, shown as
   <nama>, unverified` (atau varian null).
5. Fixture anonim: hanya nama Host/Guest/Twin, tanpa nama asli bocor.

### Kriteria terima (ambang metrik)

- Bind-once: 100% ikatan pertama bertahan terhadap bukti susulan.
- Duplikat: 100% kasus nama ganda menghasilkan null, bukan tebakan.
- Rejoin: uuid baru mendapat label berbeda dari sesi lama 100%.
- Render: 100% uuid koreksi ter-render dengan suffix `unverified`.
- Kebocoran nama asli pada fixture: `== 0`.

### Perintah verifikasi

```bash
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false uv run pytest tests/test_debrief.py -x -q
ruff check src tests evals
ruff format --check src tests evals
mypy src
```

### Di luar lingkup

- Snapshot roster per-meeting yang dipersist (sumber join roster/jurnal untuk
  jendela waktu uuid koreksi belum terkonfirmasi; builder bekerja dari bukti
  yang ada).
- Perubahan format string render di luar pola yang diasumsikan (string pasti
  belum dispesifikasi di mana pun).
- Verifikasi identitas (semua label koreksi tetap `unverified`).

---

## Tugas 6 — Pengerasan Gerbang Vacuus

### Tujuan

Gate eval koreksi tidak bisa lolos secara vacuus (hijau palsu karena data
validasi kosong); metrik promosi hanya dievaluasi pada run yang valid.

### Berkas dan fungsi yang disentuh

- `evals/test_memory_correction_eval.py`: `run_memory_correction`,
  `answer_queries` (tambah dict `validity` ke summary:
  `{changed_edges, stable_edges, fixed_edges, pre_stale_hit}`; syarat valid:
  `changed >= 1 AND stable >= 1 AND fixed >= 1 AND pre_stale_hit` hadir, bila
  tidak maka tes live GAGAL sebagai INVALID bukan lolos).
- Aturan promosi: `over_invalidation` tetap gate tetapi hanya dievaluasi bila
  valid; `entity_merge_rate` boleh dipromosikan ke `== 1.0` setelah 3 run valid
  beruntun semuanya `1.0` dengan `fixed >= 1`; `corrected_answer_accuracy`
  tetap diagnostik sampai median-of-3 `>= 0.75` pada run valid lalu jadi soft
  gate `>= 0.5`; abstention tetap diagnostik.
- Runbook Indonesia: mutakhirkan tabel headline dengan kolom/flag `valid`.
- `tests/` atau `evals/`: red test unit validitas (kasus kosong).

### Tes merah yang ditulis dulu

1. `stable_edges` kosong -> INVALID (gagal, bukan lolos).
2. `fixed_edges` kosong -> INVALID.
3. Daftar `superseded` kosong/None -> INVALID.
4. `pre_stale_hit` absen -> INVALID.
5. Kasus valid penuh -> dievaluasi normal (gate berlaku).

### Kriteria terima (ambang metrik)

- 4 kasus kosong di atas: 100% menghasilkan INVALID/gagal, 0 lolos palsu.
- `over_invalidation` pada run invalid: tidak dievaluasi (di-skip eksplisit).
- Promosi `entity_merge_rate` ke `== 1.0`: hanya setelah 3 run valid beruntun
  `1.0` dengan `fixed >= 1` (riwayat belum ada -> belum boleh promosi).
- Soft gate `corrected_answer_accuracy >= 0.5`: hanya setelah median-of-3
  `>= 0.75` pada run valid.

### Perintah verifikasi

```bash
FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=false uv run pytest evals/test_memory_correction_eval.py -x -q
ruff check src tests evals
ruff format --check src tests evals
mypy src
```

### Di luar lingkup

- Rerun live untuk membangun riwayat 3 run valid (di luar lingkup baca-saja
  laporan; dijadwalkan terpisah).
- Perubahan definisi metrik selain aturan validitas/promosi di atas.
- Auto-promosi gate tanpa bukti 3 run (dilarang eksplisit).

---

## Ledger SDD

Setiap tugas mencatat progres di `.superpowers/sdd/phase-2-followups/progress.md`
dengan format: status tugas, perintah verifikasi yang dijalankan + hasilnya,
keputusan desain, dan reviewer yang ditugaskan (satu reviewer berbeda per tugas).
