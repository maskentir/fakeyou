# Acceptance Meet latency-recovery (`lat-rec-1005`)

Tanggal: 5 Oktober 2026. Branch `feat/latency-recovery`, commit induk `96f1292`
(compact evidence v2 + trim relevansi + pin owner). Konfigurasi live:
`gemini-3.8-flash` + prefetch, penalaran default, memori Graphiti aktif
namespace `unknown_spk_owner_1006` / `unknown_spk_twin_1006`.

## Ringkasan

- 25 transkrip: 2 dibatalkan (fragmen barge-in), 22 terjawab penuh, 1
  terpotong oleh uji stop PCM (giliran 20).
- Review independen 22/22 turn terjawab: **0 severe, 22 useful**.
  Sondase c04/c26/c27/c28 semuanya netral, 0 penyangkalan baik di korpus
  (0/120 gabungan c04–c28 pada probe-trim) maupun live (0/4).
- Stop audio pemicu PCM **LULUS**: SIGINT 0,8 dtk setelah
  `synthesis_pcm_emitted`, host mendengar jawaban 20 lalu terpotong,
  worker keluar 0.
- Gate latency live **GAGAL**: e2e p50 5,05 dtk (batas ≤3), p95 8,66 dtk
  (batas ≤6). Agent masih dominan (p50 3,46 dtk); STT/TTS stabil.

## Sebelum vs sesudah

Korpus offline (default, `gemini-3.8-flash` + prefetch):

| Korpus | Token input (med) | Token penalaran (med / jumlah) | Agent p50 | Agent p95 | Severe | Useful |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| trust sebelum | 1.830 | 246 / 28.645 | 1,94 dtk | 3,40 dtk | 0 | 86/87 |
| trust sesudah | 1.784 | 229 / 25.296 | 1,96 dtk | 3,66 dtk | 0 | 87/87 |
| brief sebelum | 1.576 | 348 / 16.535 | 2,36 dtk | 3,37 dtk | 0 | 36/36 |
| brief sesudah | 1.576 | 389 / 17.333 | 2,68 dtk | 3,49 dtk | 0 | 36/36 |

Live Meet (akhir ucapan → PCM pertama, detik):

| Sesi | e2e p50 | e2e p95 | Agent p50/p95 | STT p50 | TTS p50 | Severe | Useful | c04 live |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| unknown-spk-1005 (sebelum) | 5,00 | 7,44 | 3,64 / 5,90 | 0,39 | 1,16 | — | — | 0 denial |
| lat-rec-1005 (sesudah) | 5,05 | 8,66 | 3,46 / 7,31 | 0,42 | 1,00 | 0/22 | 22/22 | 0 denial |

Probe c04 (30 sampel × c04/c26/c27/c28): sebelum 4/120 = 3,3%
[1,3%, 8,3%], sesudah (compact v2 + trim) 4/120 = 3,3%, tepat di batas.
Laju c04 **tidak memburuk**.

## Konfigurasi live terpilih

`gemini-3.8-flash` + prefetch, penalaran **default**, evidence compact v2
+ trim peringkat-relevansi + pin owner. Alasan:

- `reasoning_effort=medium` diukur lebih lambat (trust p50 2,02 lawan
  1,96 default) dan dibuang.
- Cache prompt implisit diukur 0 hit (`cache_read=0` semua turn) dan
  dibuang sebagai tuas.
- TTS stabil (chunk pertama p50 ~1 dtk live) dan bukan tuas.
- Compact v1 dibuang karena menaikkan probe 4/120 → 41/120; compact v2
  mempertahankan kalimat identitas longhand yang dipakai c04.

## Risiko

- Target e2e p50 ≤3 dtk / p95 ≤6 dtk belum tercapai; compact+trim hanya
  memangkas ~46 token input median dan tidak memulihkan latency live.
- Outlier agent 10,8 dtk terjadi tepat pada sondase c04; pola atribusi
  netral berbiaya penalaran tinggi.
- Probe di batas (4/120): 1 "bukan Rani" datar + 3 record-denial; c04
  bisa memburuk pada sampel lain.
- `CURRENT_FACTS_FILTER` membuang seluruh edge owner pada namespace live
  (Graphiti menginvalidasi owner Jumat akibat kabar burung November),
  sehingga pin owner tidak bisa menyala saat difilter pada waktu cari.
  Pra-ada, di luar lingkup, belum diperbaiki.
- Host sempat bertanya 2 giliran di luar skrip (identitas, Garuda); twin
  benar pada keduanya (peran ditegaskan, Garuda tidak dikarang).

## Gate yang belum terbukti

- Latency live p50 ≤3 dtk / p95 ≤6 dtk: **gagal** (5,05 / 8,66 dtk).
- Tuas latency berikutnya (di luar compact+trim) belum diidentifikasi
  dan belum diukur.

## Artefak

- `acceptance/report-lat-rec-1005.json`: laporan `fakeyou report` penuh.
- `acceptance/session.log`, `acceptance/stop-lat-rec-1005.json`.
- `review/acceptance-lat-rec-1005.json`: verdict per turn (teks
  terverifikasi sama dengan `record.jsonl`).
- Journal: `.runtime/audio/journals/lat-rec-1005.jsonl`.
