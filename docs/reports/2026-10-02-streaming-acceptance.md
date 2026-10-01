# Acceptance live streaming per kalimat

Tanggal: 2 Oktober 2026. Status: jalur streaming berfungsi live; sesuai kriteria stop desain, eksperimen DIHENTIKAN dan flag tetap mati. Desain: [streaming per kalimat](../plans/sentence-streaming.md). Baseline: [pengembangan berikutnya](../plans/next-development.md) (selesai), [acceptance live](../reports/2026-10-02-live-acceptance.md), [pelajaran latensi](../reports/2026-10-02-latency-lessons.md), [implementasi](2026-10-02-sentence-streaming.md).

## Ringkasan putusan

- Fungsi streaming live: lulus (34 kalimat terucap, indeks lengkap, drop generasi usang benar, stop di tengah playback dan sebelum kalimat pertama, tanpa audio susulan).
- Kualitas live: lulus (enam jenis pertanyaan benar di kedua sesi). Gate kualitas replay: tetap gagal (konfigurasi tidak berubah).
- Performa: median streaming 2,918 dtk lawan 3,243 dtk alur lama (delta dalam noise), p95 8,046 dtk lawan target 6 dtk. Milestone belum lulus.
- Stop/go: STOP. Hemat terukur (median 0,074 dtk) tidak dapat menutup gap p95 yang didominasi tunggu pra-teks (~94%). `FAKEYOU_CONVERSATION__SENTENCE_STREAMING` tetap `false`.

## Sesi 5: `perf-1002-s5` (flag nyala)

Twin admitted; 16 giliran (9 spoken, 5 interrupted, 2 cancelled), 18 kalimat, 17 event kalimat, 8 sela interim, 1 `sentence_dropped` (`stale_generation`, benar). Stop saat PCM kalimat 0 mengalir: PCM 21:11:41,20 → `agent_cancelled` + `playback_dropped` 21:11:41,89 → `session_finished` kode 0, tanpa audio susulan. Ingestion 4/4 jendela, 14 fakta, tanpa fault. Nol fault percakapan/sintesis.

## Sesi 6: `perf-1002-s6` (flag nyala)

12 giliran (9 spoken, 1 interrupted, 1 silent, 1 cancelled), 16 kalimat, 3 sela. Stop sebelum kalimat pertama: `agent_started` 21:23:02,35 → `agent_cancelled` 21:23:03,36, tanpa event teks/kalimat/playback untuk giliran itu; host mengonfirmasi twin tidak sempat bersuara. Ingestion sempat timeout dua kali (Meta), lalu selesai 4/4 jendela, 11 fakta, via retry. Worker kode 0.

## Angka koreksi vs baseline

Laporan mentah sempat mengaitkan PCM ke giliran yang salah pada alur streaming (tiap giliran mendapat PCM giliran berikut) karena `agent_finished` tercatat setelah seluruh speak. Atribusi dikoreksi offline per request (seluruh gap sintesis→PCM senyatanya 0,5–1,5 dtk) lalu diperbaiki permanen lewat `transcript_id` pada event sintesis (commit `da09b49`, 5 regresi merah-hijau; angka S1 tidak berubah: 3,243/8,597).

| Alur | n utuh | Median | p95 | Maks |
| --- | --- | --- | --- | --- |
| Lama S1+S2 | 23 | 3,243 | 8,319 | 8,93 |
| Streaming S5+S6 | 18 | 2,918 | 8,046 | 8,768 |
| Kalimat-0 → PCM streaming | 18 | 1,081 | 2,084 | 2,138 |

Delta median −0,33 dtk berada dalam variansi provider malam itu (p95 replay 6,63 → 12,38 pada konfigurasi sama), sehingga tidak terbukti bermanfaat. Teks pertama live tetap tiba di ~94% waktu respons (probe n=6: hemat median 0,074 dtk, maks 0,355 dtk).

## Regresi dan gate

- Ruff, format (117 berkas), mypy bersih; pytest non-live/non-container 1.559 lulus; container build + tes lulus; image terverifikasi cocok HEAD dan memuat kode streaming.
- Replay flag-mati di tree baru: gagal sesuai prediksi (p95 12,376 dtk), seluruh gate kualitas lulus, ambang tidak berubah. Replay flag-nyala tidak dijalankan karena harness memakai responder lama (flag tidak berpengaruh); delta streaming hanya terukur live.
- Harness replay belum mengenal field stream per giliran (Task 6 desain) — terbuka bila eksperimen dilanjutkan.

## Konfirmasi host

Host menjawab ya: jawaban sesi 5–6 utuh dan jelas; suara berhenti saat disela maupun stop tanpa susulan; twin sesi 6 tidak bersuara setelah pertanyaan terakhir.

## Artefak

Journal dan catatan: `.runtime/audio/journals/perf-1002-s{5,6}.jsonl`, `.runtime/audio/meetings/perf-1002-s{5,6}/record.jsonl`. Ringkasan: `.runtime/acceptance-2026-10-02/` (`report-s5.json`, `report-s6.json`). Skrip koreksi: `/tmp/correct_stream.py`. Persona/namespace uji dipakai ulang (`twin_f4440c7b-…`); `.env` dan data `acc2`/`acc3` tidak disentuh.

## Batas bukti dan catatan proses

- Sampel streaming utuh n=18 (< 20 sendirian); gabungan 41 jawaban dua alur.
- Pada giliran streaming, tahap berbasis `agent_finished` mencakup waktu speak (struktur sequential); perbandingan model-vs-model memakai `llm_duration_seconds` yang setara.
- Satu galat parse CLI `--owner` yang tidak terjelaskan (berhasil dengan `--owner-id`); ingestion Meta timeout tiga kali malam itu lalu pulih.
- Sesi sejawat `straw-aries` melakukan cleanup legacy di tree yang sama secara paralel; fix ini di-commit per-hunk (`da09b49`) tanpa menyentuh 29 file kerjanya, dan docs user yang belum di-commit dikembalikan utuh.
