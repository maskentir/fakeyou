# Ukur live Fase 1 (inversi, endpointing, latensi)

Tujuan: menghasilkan angka yang menentukan desain Fase 1 butir 12
(watermark + holdback vs record-repair) dan bukti latensi formal
pasca-Fase 0. Sesi ini observasi murni — tanpa ubah kode.

## Kebutuhan

- Host + sedikitnya dua manusia di ruangan berbeda (beda kamar, mic nyala).
- Satu twin listener di container worker (jangan browser Mac):
  `fakeyou worker start ... --audio --converse --persona .runtime/audio/persona.json`
  dengan `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED=true` di `.env` live.
- Backup `.env` sebelum sesi; jangan tampilkan secret; jangan ubah artefak
  `.runtime` lama.

## Skrip sesi (±20 menit)

1. **Babak A — solo bergantian (5 menit).** Manusia 1 lalu 2 bergantian
   satu kalimat pendek per giliran (±10 giliran/orang). Baseline
   endpointing per stream.
2. **Babak B — overlap terencana (5 menit).** Kedua manusia mulai bicara
   bersamaan (±2-3 detik overlap) lalu satu berhenti; 6-8 kali. Memicu
   final lintas-stream yang berdekatan — kandidat inversi urutan.
3. **Babak C — angka pendek (5 menit).** Bergantian mengucap digit
   0-9 satu kata per giliran (±10 giliran/orang). Mengukur efek vocab
   0.8; dinilai offline dengan `utterance_quality.score_script`.
4. **Babak D — hening + satu ucapan (5 menit).** Hening penuh 60 detik,
   lalu satu manusia mengucap satu kalimat. Observasi I10
   (ucapan pertama pasca-hening).

## Analisis

```sh
fakeyou report --meeting-id MEETING_ID
```

Dari JSON laporan, baca kunci `stt_quality` dan `latency`
(misalnya disaring dengan `jq`). Yang dibaca:

- `stt_quality.order_inversions` — jumlah inversi vs total giliran.
- `stt_quality.turn_interims` — churn interim (p50/p95).
- `stt_quality.first_interim_to_final_ms` — distribusi keterlambatan final.
- `stt_quality.capture_engines` — bauran decode vs mstp.
- `latency.speech_stopped_to_final_seconds` — delay endpointing.
- `latency.speech_stopped_to_first_pcm_seconds` — bukti latensi agent
  (bandingkan ke angka acceptance pra-Fase 0).

## Kriteria keputusan (usul, diputus setelah data)

- Inversi > 1% giliran atau p95 keterlambatan final > 300 ms →
  watermark + holdback terbatas (butir 12).
- Inversi nihil dan p95 < 100 ms → publish-segera + repair-on-read.
- Di antaranya → holdback adaptif kecil + `order_inversion_detected`
  sebagai alarm lanjutan.

Hasil sesi dicatat di adendum laporan acceptance sebelum cabang
implementasi Fase 1 dibuka.
