# Acceptance live kinerja percakapan

Tanggal: 2 Oktober 2026. Status: fungsi live lulus; milestone performa belum lulus. Rencana: [pengembangan berikutnya](../plans/next-development.md). Laporan terkait: [Task 4](2026-10-01-conversation-performance.md), [integrasi](2026-10-02-conversation-performance-integration.md), [acceptance refactor](2026-10-01-refactor-acceptance.md).

## Lingkungan

- Branch `feat/conversation-performance`, commit `6d9a221`. Image `fakeyou-meeting-worker:local` dibangun dari source yang identik byte dengan HEAD (selisih hanya docs di luar `.dockerignore`); Neo4j dan embeddings healthy.
- Jawaban memakai MiniMax-M3, TTS MiniMax, STT Muse, ekstraksi memori `muse-spark`, dan embedding Granite lokal. Tidak ada fallback jawaban aktif.
- Persona uji `Sari Twin` (`perf_owner`/`perf_twin`, Bahasa Indonesia) dengan namespace memori terpisah `twin_f4440c7b-…`; tidak ada baca atau tulis data `acc2`/`acc3`. Owner note uji: demo customer Melati, Jumat 10 Oktober 2026 jam tiga sore, PIC Dewi.
- Link Meet: [idw-naga-qfk](https://meet.google.com/idw-naga-qfk), dipakai ulang untuk empat admission; semuanya admitted.

## Sesi 1: `perf-1002-s1`

Twin admitted 28,9 detik setelah permintaan; audio siap sebelum admission. Host menanyakan seluruh jenis pertanyaan tetap: fakta sederhana, owner note, kata ganti, tidak diketahui, dan di luar kewenangan, plus pengulangan.

| Bukti journal | Hasil |
| --- | --- |
| Transkrip final / giliran agent | 22 / 22 |
| Hasil giliran | 16 spoken, 4 interrupted, 1 cancelled, 1 silent |
| Pemicu interupsi / stop terukur | 4 interim; median 1,215 dtk, maks 1,364 dtk |
| Stop saat model bekerja | `agent_started` 19:05:57,04 → `agent_cancelled` 19:05:57,74 → `session_finished` `left_by_owner`, kode 0 |
| Audio susulan setelah stop | Tidak ada |
| Ingestion | 2/5 jendela lalu timeout Meta; dilanjutkan `memory-ingest` menjadi 5/5, 15 fakta, `completed: true` |

Jawaban memakai fakta terverifikasi, owner note (3 recall), konteks kata ganti ("dia" → Sari), penolakan hal tidak diketahui, dan penolakan persetujuan budget tanpa otoritas tertulis.

## Sesi 2: `perf-1002-s2`

| Bukti journal | Hasil |
| --- | --- |
| Transkrip final / giliran agent | 16 / 16 |
| Hasil giliran | 8 spoken, 8 interrupted |
| Pemicu interupsi / stop terukur | 7 interim; median 1,096 dtk, maks 1,499 dtk |
| Recall meeting sebelumnya | Tool recall mengembalikan fakta owner note dan transkrip `perf-1002-s1`; jawaban menyebut jadwal demo |
| Stop | SIGINT 0,7 dtk setelah `playback_started`, sebelum PCM pertama; sesi berakhir kode 0 tanpa audio susulan |
| Ingestion | 4 episode, 12 fakta, tanpa fault |

Delapan giliran interupsi berasal dari tumpang tindih ucapan host; semuanya dilaporkan terpisah dan tidak dihapus dari sampel.

## Sesi 3–4: uji stop tambahan

- `perf-1002-s3`: SIGINT mendarat tepat saat playback selesai (batas 0,2–0,4 dtk); tidak dipakai sebagai bukti stop saat audio.
- `perf-1002-s4`: PCM pertama 19:26:42,17 (235 karakter antre) → `playback_dropped` 19:26:43,14 → `session_finished` kode 0. Host mendengar twin mulai berbicara sebelum berhenti. Tidak ada audio susulan. Ini bukti stop saat audio berjalan.

## Hasil fungsi, kualitas, dan performa

- Fungsi: startup, admission, recall lintas sesi, kata ganti, interupsi (11 pemicu), stop saat model bekerja, stop saat audio berbunyi, dan ingestion lengkap. Lulus.
- Kualitas live: enam jenis pertanyaan dijawab benar sesuai catatan meeting; tidak ada jawaban usang setelah stop. Observasi: model tiga kali menyapa host sebagai "Sam" tanpa sumber di persona maupun transkrip. Gate kualitas replay tetap gagal (Task 4, konfigurasi tidak berubah). Kualitas milestone belum lulus.
- Performa: 23 jawaban utuh dari dua sesi; median 3,243 dtk (target ≤ 3), p95 8,319 dtk (target ≤ 6), maks 8,93 dtk. Giliran interupsi ber-PCM: 10 sampel, median 3,039 dtk. Interupsi tetap dalam batas pembanding (maks 1,364/1,499 dtk lawan 1,618/1,833 dtk). Milestone performa belum lulus.

| Tahap (report module) | S1 p50 | S1 p95 | S2 p50 | S2 p95 |
| --- | --- | --- | --- | --- |
| Akhir ucapan → transkrip final | 0,369 | 0,573 | 0,324 | 0,787 |
| Final → agent mulai | 0,002 | 0,004 | 0,001 | 0,002 |
| Agent → balasan | 2,541 | 6,758 | 1,449 | 8,268 |
| Balasan → PCM pertama | 1,091 | 2,487 | 1,159 | 2,071 |
| Akhir ucapan → PCM pertama | 3,243 | 8,597 | 2,864 | 10,835 |

Waktu model tetap dominan; hasil mengulang gap yang sama pada konfigurasi yang tidak berubah, sesuai prediksi laporan negatif Task 4. Langkah desain berikut tetap streaming teks per kalimat sebelum perubahan alur.

## Konfirmasi host

Host menjawab ya untuk ketiga hal: jawaban terdengar utuh dan jelas pada sesi 1–2; suara berhenti saat disela dan tidak ada suara susulan setelah tiap sesi; twin sesi 4 terdengar mulai berbicara sebelum berhenti.

## Artefak

Journal dan catatan: `.runtime/audio/journals/perf-1002-s{1,2,3,4}.jsonl`, `.runtime/audio/meetings/perf-1002-s{1,2,3,4}/record.jsonl`. Ringkasan: `.runtime/acceptance-2026-10-02/` (`report-s1.json`, `report-s2.json`, `report-s3.json`, `live-config.json` tersamar, `stop-evidence.json`). Persona dan owner note uji: `.runtime/audio/persona-perf.json`, namespace `twin_f4440c7b-…`.

## Batas bukti

- Endpoint Meta tidak terjangkau ±5 menit saat seeding (pulih sendiri); satu jendela ingestion S1 timeout lalu selesai via resume. Tidak ada retry yang mengubah data.
- Stop sesi 2 mendarat sebelum PCM dan stop sesi 3 di batas akhir playback; keduanya dicatat sebagai bukti parsial dan digantikan sesi 4.
- Model menyapa "Sam" tanpa sumber; tidak ada gate persona untuk sapaan nama.
- Satu host; atribusi dua pembicara belum diuji.
