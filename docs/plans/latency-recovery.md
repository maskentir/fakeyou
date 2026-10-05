# Plan pemulihan latency live tanpa mengorbankan c04

> **Untuk agentic worker:** eksekusi task berurutan dengan checkbox. Orkestrator menjalankan sendiri command verifikasi sebelum menerima hasil pekerja; reviewer semantik terpisah dari penulis.

**Tujuan:** menurunkan akhir ucapan → PCM pertama di Meet ke p50 ≤3 s dan p95 ≤6 s dengan `gemini-3.8-flash` + prefetch pada penalaran default, sambil mempertahankan semua gate semantik yang kini lulus dan laju penyangkalan c04 yang sudah turun.

**Status:** rencana, 5 Oktober 2026. Basis `master` pada `cc447ab`. Buat branch `feat/latency-recovery`. Jangan push/merge.

## Bukti awal

- [Laporan unknown-speaker](../reports/2026-10-05-unknown-speaker.md): untuk pertama kali semua gate semantik lulus pada penalaran default (trust 0 severe, 86/87; brief 0 severe, 36/36). Penyangkalan c04 turun dari 37,5% ke 3,3% lewat `speaker_identity` eksplisit di setiap sumber evidence (`src/fakeyou/provenance.py`) dan label ingestion baru. Penalaran `low` tetap gagal (c04 57,5%, trust 2 severe).
- [Acceptance Meet unknown-speaker](../reports/2026-10-05-meet-acceptance-unknown-speaker.md): akhir ucapan → PCM pertama p50 5,18 s / p95 7,43 s (gate gagal). Agent p50 3,77 / p95 5,93 s, lawan 2,12 / 3,35 s pada sesi `live-trust-1005b` sebelum perbaikan identitas. TTS ~1,2 s dan STT ~0,4 s setara. Token penalaran trust naik dari median ~223 ke ~330 per giliran.
- Evidence memotong ke 5 fakta dengan urutan owner note dulu; fakta meeting yang relevan bisa terpotong (terlihat di giliran 2 acceptance).
- Streaming per kalimat sudah dua kali terbukti tidak membantu (teks pertama di ≥94% durasi jawaban) dan tetap mati.
- Alat yang sudah ada: probe c04 `evals/test_speaker_probe_eval.py` (30 sampel per variasi, klasifier teraudit), `token_usage` per panggilan di harness, `fakeyou report` untuk pemecahan latency live, skrip watcher stop PCM di `.runtime/unknown-speaker/acceptance/`.

## Batasan global

- Tanpa dependency baru atau perubahan lockfile. Python Inggris tanpa komentar/docstring dan tanpa string Bahasa Indonesia di `.py`; dokumentasi dan fixture boleh Bahasa Indonesia.
- `.env` boleh diubah untuk konfigurasi pengembangan setelah backup `.env.backup-<tanggal>`; jangan menampilkan secret. Jangan mengubah backup lama, persona privat, atau artefak `.runtime` lama. Run baru di `.runtime/latency-recovery/`.
- Gate tidak dilonggarkan: brief dan trust 0 severe dan ≥90% useful, p95 agent ≤4 s, live akhir ucapan → PCM pertama p50 ≤3 s / p95 ≤6 s. Laju penyangkalan probe c04 pada konfigurasi terpilih tidak boleh lebih buruk dari 3,3% (default saat ini) dan c04 sendiri tetap 0/30.
- Tidak menambah model penilai, router, atau penulis ulang di setiap jawaban live. `brief_only` tetap tanpa memori; evidence tetap data, bukan izin.

## Task 0 — Baseline dan pemecahan waktu

- [ ] Catat HEAD/status; jalankan gate statis dan `pytest -m 'not live and not container'`.
- [ ] Ukur corpus trust default saat ini dengan `token_usage`: token input, token penalaran, token output, dan durasi per panggilan. Bandingkan dengan artefak `.runtime/live-trust-latency/` (sebelum perbaikan identitas). Pastikan penyebab regresi agent adalah ukuran evidence dan/atau token penalaran, bukan hal lain.
- [ ] Catat ukuran evidence prefetch (karakter dan token) per giliran, termasuk jumlah fakta dan sumber.

## Task 1 — Ringkas evidence

- [ ] Tes merah untuk bentuk render baru, schema evidence, dan semua tes provenance/prefetch yang ada.
- [ ] Kandidat ringkas yang diuji terpisah:
  - catatan identitas penutur tak terverifikasi ditulis sekali di header tool result, dan sumber cukup membawa penanda singkat, alih-alih string panjang di setiap sumber;
  - field yang tidak dipakai model untuk menjawab (misalnya `episode_id`, `time_granularity`, `recorded_at` bila sama dengan `source_at`) dibuang dari render ke model tetapi tetap ada di artefak eval;
  - evidence giliran lanjutan yang dipertahankan dibatasi ukurannya.
- [ ] Ukur setiap kandidat dengan probe c04 (≥30 sampel per variasi, default) dan corpus trust default. Kandidat hanya diterima bila laju c04 tidak memburuk dan token input/penalaran turun.

## Task 2 — Pemotongan fakta yang tetap relevan

- [ ] Tes merah: ketika fakta meeting yang paling relevan dengan pertanyaan berada di luar 5 owner note pertama, fakta itu tetap sampai ke model.
- [ ] Ganti urutan potong owner-first murni dengan aturan yang mempertimbangkan relevansi dari pencarian, tanpa menghapus prioritas owner note untuk konflik. Pertahankan batas ukuran evidence dari Task 1.

## Task 3 — Penalaran dan cache

- [ ] Ukur `reasoning_effort=medium` dengan perbaikan data saat ini: probe c04 dan corpus trust/brief. Terima hanya bila lulus 0 severe dan laju c04 tidak memburuk.
- [ ] Ukur apakah cache prompt implisit Gemini aktif (`input_token_details` di `token_usage`) untuk system prompt statis; bila tidak, cari tahu syaratnya dari SDK terpasang dan dokumentasikan. Jangan menambah cache eksplisit berbayar tanpa bukti manfaat.

## Task 4 — TTS

- [ ] Ukur waktu jawaban → PCM pertama MiniMax TTS per panjang kalimat dari journal live dan sondase lokal. Bandingkan opsi yang tersedia tanpa dependency baru (model TTS lain pada provider yang sama, panjang potongan pertama). Laporkan bila tidak ada opsi yang lebih cepat; jangan ubah suara atau provider tanpa bukti kualitas Bahasa Indonesia.

## Task 5 — Corpus, review, dan acceptance Meet

- [ ] Jalankan corpus brief dan trust untuk konfigurasi kandidat terbaik ke `.runtime/latency-recovery/<run>/`; review semantik independen seluruh turn dengan standar `.runtime/meeting-delegation/resume-a-r4-review.md`.
- [ ] Gate statis + non-live/non-container, build image, `pytest -m container tests/test_worker_image.py`.
- [ ] Acceptance Meet dengan memori aktif: sedikitnya 20 jawaban dengan konfirmasi host, termasuk pertanyaan c04, pertanyaan lanjutan yang butuh fakta meeting di luar owner note, percobaan approve, dan stop audio yang dipicu PCM. Laporkan akhir ucapan → PCM pertama p50/p95 dengan pemecahan STT/agent/TTS dari `fakeyou report`, dan nyatakan lulus/gagal terhadap gate live.
- [ ] Minta link Meet baru hanya setelah offline, provider, dan container siap.
- [ ] Commit lokal per task yang direview. Akhiri dengan daftar commit, command + exit code, artefak, tabel gate (semantik, c04, agent p95, live p50/p95), dan risiko.

## Prompt siap salin untuk Muse

```text
Kerjakan docs/plans/latency-recovery.md di repo /Users/pushmove/Desktop/jemboed/fakeyou.

Baca plan itu penuh, docs/reports/2026-10-05-unknown-speaker.md, docs/reports/2026-10-05-meet-acceptance-unknown-speaker.md, dan .runtime/unknown-speaker/status.json sebelum mulai. Buat branch feat/latency-recovery dari master terbaru (sudah memuat plan ini); jangan reset, buang perubahan, push, atau merge.

Tujuan: turunkan latency live (akhir ucapan ke PCM pertama p50 <= 3 s, p95 <= 6 s) untuk gemini-3.8-flash + prefetch pada penalaran default tanpa merusak gate semantik yang kini lulus dan laju penyangkalan c04 yang sudah turun ke 3,3%. Regresi terakhir ada di agent (p50 3,77 s lawan 2,12 s sebelum perbaikan identitas penutur), kemungkinan karena evidence lebih panjang dan token penalaran naik. Ringkas evidence, pastikan fakta meeting yang relevan tidak terpotong oleh batas 5 fakta owner-first, ukur reasoning_effort=medium dan cache prompt implisit, ukur TTS, lalu ulang acceptance Meet.

Aturan: Python Inggris tanpa komentar/docstring baru dan tanpa string Bahasa Indonesia di .py; dokumentasi Bahasa Indonesia; tanpa dependency baru; boleh mengubah .env untuk konfigurasi pengembangan setelah backup dan tanpa menampilkan secret; jangan mengubah artefak lama; brief_only tetap tanpa memori; gate kualitas dan latency tidak dilonggarkan dan gate live harus dilaporkan lulus atau gagal; laju c04 tidak boleh memburuk; tanpa xfail; tanpa model penilai atau penulis ulang pada setiap jawaban live.

Tulis tes merah sebelum implementasi. Jalankan sendiri setiap command verifikasi; klaim hijau tanpa output aktual bukan bukti. Corpus live memakai direktori baru di .runtime/latency-recovery/ dan direview independen seluruh turn dengan standar severity .runtime/meeting-delegation/resume-a-r4-review.md. Catat progres di .runtime/latency-recovery/status.json (task, status, command, exit code, artefak, gate terbuka); setelah compaction baca file itu sebelum lanjut.

Akhiri dengan commit lokal yang direview, tabel sebelum/sesudah (token input, token penalaran, agent p50/p95, live p50/p95 dengan pemecahan STT/agent/TTS, severe, useful, laju c04), konfigurasi live terpilih, risiko, dan gate yang belum terbukti. Minta link Meet baru hanya setelah offline, provider, dan container siap.
```

Jalankan:

```sh
mkdir -p .runtime/latency-recovery
muse exec --workspace /Users/pushmove/Desktop/jemboed/fakeyou --json --prompt-file .runtime/latency-recovery/muse-prompt.txt
```

Salin blok prompt ke `.runtime/latency-recovery/muse-prompt.txt` terlebih dahulu; periksa `muse exec --help` bila opsi berbeda.
