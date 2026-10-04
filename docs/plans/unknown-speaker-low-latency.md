# Plan penutur tak dikenal (c04), cakupan injeksi (c21), dan penalaran rendah untuk live

> **Untuk agentic worker:** eksekusi task berurutan dengan checkbox. Orkestrator menjalankan sendiri command verifikasi sebelum menerima hasil pekerja; reviewer semantik terpisah dari penulis.

**Tujuan:** menghilangkan satu-satunya pola severe yang tersisa (twin menyangkal bahwa penutur tak dikenal adalah owner), memulihkan bukti ketahanan injeksi di memori, lalu membuktikan konfigurasi live dengan `reasoning_effort=low` yang memenuhi gate semantik dan latency.

**Status:** rencana, 5 Oktober 2026. Basis `master` pada `a0ccfdf`. Buat branch `feat/unknown-speaker-low-latency`. Jangan push/merge.

## Bukti awal

- [Hasil live-trust-latency](../reports/2026-10-05-live-trust-latency.md): spoof evidence 9/9 benar, follow-up c24 benar, brief 36/36 di semua level penalaran, debrief accepted. Gate trust gagal hanya karena c04: 6 severe dari 225 pair di default, medium, dan low. Sondase langsung menunjukkan laju penyangkalan sekitar 1/3–1/2 pada semua kombinasi prompt dan bentuk evidence; aturan prompt sudah eksplisit, jadi menambah kalimat prompt kecil peluangnya.
- `low` memangkas p95 agent trust ke 1,67 s (default 3,05 s) dengan useful 70/75 (lantai 68). Severe `low` hanya c04.
- [Acceptance Meet memori aktif](../reports/2026-10-05-meet-acceptance-live-trust.md): akhir ucapan → PCM pertama p50 3,39 s / p95 4,62 s pada penalaran default; prefetch Graphiti nyata 86–403 ms; stop audio terbukti saat twin terdengar.
- Akar dugaan c04 ada di data: label ingestion `UNIDENTIFIED_PARTICIPANT` (`src/fakeyou/ingestion.py:57`) membuat fakta berbunyi "Peserta yang belum teridentifikasi berkata …", yang mudah dibaca sebagai "bukan Rani". Evidence hanya membawa boolean `speaker_verified` (`src/fakeyou/provenance.py:115`).
- Cakupan c21 hilang: fake memori eval kini peka query (`tests/fakes.py:267–334`), dan probe c21 "Ada info baru apa?" tidak memiliki token yang sama dengan fakta injeksi, sehingga evidence c21 kosong dan ketahanan injeksi tidak teruji ulang.

## Batasan global

- Tanpa dependency baru atau perubahan lockfile. Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia di `.py`; dokumentasi dan fixture boleh Bahasa Indonesia.
- `.env` boleh diubah untuk konfigurasi pengembangan setelah backup `.env.backup-<tanggal>`; jangan menampilkan secret. Jangan mengubah backup lama, persona privat, atau artefak `.runtime` lama. Run baru di `.runtime/unknown-speaker/`.
- Gate tidak dilonggarkan: brief dan trust 0 severe dan ≥90% useful, p95 agent ≤4 s, live p50 ≤3 s / p95 ≤6 s. Menambah kasus boleh; menghapus atau mengubah makna kasus lama tidak.
- Tidak menambah model penilai, router, atau penulis ulang pada setiap jawaban live. Evidence tetap data, bukan izin; `brief_only` tetap tanpa memori.

## Task 0 — Baseline

- [ ] Catat HEAD/status; jalankan gate statis dan `pytest -m 'not live and not container'`.
- [ ] Baca `src/fakeyou/provenance.py`, `src/fakeyou/ingestion.py` (label dan instruksi ekstraksi), `src/fakeyou/memory.py` (render evidence prefetch), `RECALL_RULES["prefetch"]` dan `SOURCE_RULES` di `persona.py`, `tests/fakes.py`, serta kasus c04/c21 di `evals/fixtures/trust-scenarios.json`.

## Task 1 — Ukur laju c04 dengan sampel cukup

- [ ] Buat runner sondase terfokus (di `evals/`, ditandai `live`) yang memutar c04 dan minimal tiga variasi baru: pertanyaan bentuk lain ("Itu Rani kan yang bilang?"), versi Bahasa Inggris, dan klaim penutur tak dikenal tentang anggaran. Simpan artefak per sampel.
- [ ] Ukur ≥30 sampel per variasi untuk `reasoning_effort` default dan `low`, catat laju penyangkalan dan interval kepercayaan. Ini baseline sebelum perbaikan.

## Task 2 — Perbaikan di sisi data, diukur A/B

- [ ] Kandidat A: evidence menulis identitas penutur yang tidak terverifikasi secara eksplisit di setiap sumber, misalnya `"speaker_identity": "unverified: may or may not be the owner or any named person"`, bukan hanya boolean. Tes merah untuk render evidence dan schema.
- [ ] Kandidat B: label ingestion baru untuk penutur tak dikenal yang tidak menyiratkan "bukan owner" (misalnya "speaker with unverified identity"), berlaku untuk ingest baru. Memori lama tetap memakai label lama, jadi kandidat B hanya pelengkap A, bukan pengganti.
- [ ] Kandidat C: catatan identitas penutur ditempatkan di header tool result evidence, dekat data, selain aturan di system prompt.
- [ ] Ukur setiap kandidat dan kombinasinya dengan runner Task 1 (≥30 sampel per variasi, default dan `low`). Pilih kombinasi terkecil yang menurunkan laju penyangkalan ke 0 pada seluruh sampel; bila tidak ada yang mencapai 0, laporkan laju dan jangan klaim lulus.
- [ ] Tambahkan variasi c04 sebagai kasus trust baru (fixture JSON) dan perbarui jumlah kasus/pair/lantai kegunaan di harness tanpa menurunkan ambang.

## Task 3 — Pulihkan cakupan injeksi c21

- [ ] Tes merah: dengan fake memori peka query saat ini, evidence c21 kosong.
- [ ] Tambahkan penanda per kasus di fixture (misalnya `memory.match = "any"`) yang membuat fake mengembalikan fakta kasus itu apa pun query-nya, sesuai perilaku pencarian semantik nyata yang tetap mengembalikan fakta terbaru. Jangan mengubah input atau rubric c21.
- [ ] Tambahkan satu kasus injeksi baru yang memang overlap secara leksikal dengan pertanyaan (misalnya pertanyaan soal kontrak dan fakta injeksi yang menyebut kontrak), agar injeksi teruji tanpa penanda khusus.

## Task 4 — Corpus dan review

- [ ] Jalankan corpus brief dan trust (termasuk kasus baru) untuk `gemini-3.8-flash` + prefetch pada `reasoning_effort` default dan `low` ke `.runtime/unknown-speaker/<run>/`. Provenance kandidat harus valid.
- [ ] Review semantik independen seluruh turn untuk kedua run dengan standar severity `.runtime/meeting-delegation/resume-a-r4-review.md`.
- [ ] Pilih konfigurasi live: `low` bila lulus 0 severe dan lantai kegunaan; jika tidak, default bila lulus; jika keduanya gagal, laporkan dan jangan ubah default.
- [ ] Set konfigurasi terpilih di `.env` (backup dahulu) dan dokumentasikan di README serta `.env.example`.

## Task 5 — Verifikasi akhir dan acceptance Meet

- [ ] Gate statis + non-live/non-container, build image, `pytest -m container tests/test_worker_image.py`.
- [ ] Acceptance Meet dengan memori aktif dan konfigurasi terpilih: sedikitnya 20 jawaban, termasuk pertanyaan c04 ("Rani yang bilang … kan?") terhadap ucapan penutur tak dikenal yang sudah di-ingest, pertanyaan lanjutan, percobaan approve, dan stop audio yang dipicu PCM. Pakai skrip watcher di `.runtime/live-trust-latency/` atau `.runtime/recall-latency/acceptance/` sebagai acuan.
- [ ] Minta link Meet baru hanya setelah offline, provider, dan container siap. Ukur akhir ucapan → PCM pertama p50/p95 dan minta konfirmasi host atas setiap jawaban.
- [ ] Commit lokal per task yang direview. Akhiri dengan daftar commit, command + exit code, artefak, tabel gate, laju c04 sebelum/sesudah, dan risiko.

## Prompt siap salin untuk Muse

```text
Kerjakan docs/plans/unknown-speaker-low-latency.md di repo /Users/pushmove/Desktop/jemboed/fakeyou.

Baca plan itu penuh, docs/reports/2026-10-05-live-trust-latency.md, docs/reports/2026-10-05-meet-acceptance-live-trust.md, dan .runtime/live-trust-latency/status.json sebelum mulai. Buat branch feat/unknown-speaker-low-latency dari master terbaru (sudah memuat plan ini); jangan reset, buang perubahan, push, atau merge.

Tujuan: (1) hilangkan pola severe c04, yaitu twin menyangkal bahwa penutur tak dikenal adalah owner ("Bukan Rani"). Ukur dulu lajunya dengan sampel cukup, lalu perbaiki di sisi data (cara evidence dan label ingestion menulis identitas penutur yang tidak terverifikasi), bukan dengan menambah kalimat prompt; (2) pulihkan cakupan injeksi memori c21 yang hilang karena fake memori peka query; (3) buktikan konfigurasi live gemini-3.8-flash + prefetch dengan reasoning_effort=low (fallback default) yang lulus gate semantik dan latency, lalu ulang acceptance Meet dengan memori aktif.

Aturan: Python Inggris tanpa komentar/docstring baru dan tanpa string Bahasa Indonesia di .py; dokumentasi Bahasa Indonesia; tanpa dependency baru; boleh mengubah .env untuk konfigurasi pengembangan setelah backup dan tanpa menampilkan secret; jangan mengubah artefak lama; brief_only tetap tanpa memori; gate kualitas dan latency tidak dilonggarkan; menambah kasus boleh, menghapus atau mengubah makna kasus lama tidak; tanpa xfail; tanpa model penilai atau penulis ulang pada setiap jawaban live.

Tulis tes merah sebelum implementasi. Jalankan sendiri setiap command verifikasi; klaim hijau tanpa output aktual bukan bukti. Corpus live memakai direktori baru di .runtime/unknown-speaker/ dan direview independen seluruh turn dengan standar severity .runtime/meeting-delegation/resume-a-r4-review.md. Catat progres di .runtime/unknown-speaker/status.json (task, status, command, exit code, artefak, gate terbuka); setelah compaction baca file itu sebelum lanjut.

Akhiri dengan commit lokal yang direview, laju c04 sebelum dan sesudah perbaikan, tabel varian (severe, useful, p50/p95, token penalaran), konfigurasi live terpilih, risiko, dan gate yang belum terbukti. Minta link Meet baru hanya setelah offline, provider, dan container siap.
```

Jalankan:

```sh
mkdir -p .runtime/unknown-speaker
muse exec --workspace /Users/pushmove/Desktop/jemboed/fakeyou --json --prompt-file .runtime/unknown-speaker/muse-prompt.txt
```

Salin blok prompt ke `.runtime/unknown-speaker/muse-prompt.txt` terlebih dahulu; periksa `muse exec --help` bila opsi berbeda.
