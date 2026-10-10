# Runbook Diarisasi Koreksi (Fase B)

Koreksi pasca-sesi untuk atribusi speaker ("siapa bilang apa") memakai
endpoint file Muse Voice Transcribe mode `DIARIZATION`. Job ini tidak
pernah menulis `record.jsonl`; hasilnya dua sidecar di direktori sesi:

- `diarization.rttm` — giliran bicara format RTTM.
- `diarization.json` — skema `diarization_correction/v1`: `schema`,
  `record_sha256`, `turns[]` berisi `start_ms`/`end_ms`/`speaker`/
  `corrected_uuid`. Tanpa teks transkrip.

## Kapan menjalankan

Jalankan manual setelah sesi selesai dan audio per-slot Fase A lengkap:

```sh
fakeyou diarize run --meeting-id m1
```

Prasyarat: `FAKEYOU_STT__API_KEY` di `.env`, WAV per-slot selaras
(`seq_start` sama dan nol), dan tidak ada operasi lain mengunci sesi
yang sama (kunci `diarization` per sesi; run kedua ditolak).

## Biaya

Biaya API sekitar ±$0,18/jam audio. Satu sesi pendek biasanya jauh di
bawah itu; tagihan mengikuti durasi audio yang dikirim.

## Membaca hasil koreksi

Satu-satunya choke point baca adalah `load_corrected_record` dari
`src/fakeyou/diarize_correction.py`. Ia mengembalikan entri
`load_meeting_record` biasa, dengan tiap entri transkrip yang titik
tengah `capture_ms`-nya jatuh dalam rentang giliran mendapat
`corrected_speaker_uuid`; entri di luar semua rentang (lebih dari
`tolerance_ms`, bawaan 2000) tetap `None`. Waktu giliran relatif
terhadap WAV campuran; offset awal sesi dibaca dari `anchors` di
`audio/slots.json`.

Untuk ringkasan cepat tanpa kode:

```sh
fakeyou diarize show --meeting-id m1
```

Contoh keluaran:

```text
turns: 2
covered: 3/4 transcript entries
status: ok
```

## Perilaku stale

Sidecar menyimpan `record_sha256` dari bita `record.jsonl` saat koreksi
ditulis. Bila record berubah sesudahnya, `load_corrected_record`
menolak dengan `DiarizeError` stale dan `diarize show` keluar dengan
kode 2. Perbaikannya: jalankan ulang `diarize run` untuk sesi itu.
Sidecar yang rusak (skema tak dikenal atau turn tak valid) bukan stale:
`diarize show` menolak dengan kode 1 tanpa label stale.

Bila sidecar belum ada, `load_corrected_record` mengembalikan record
asli apa adanya, sedangkan `diarize show` menolak karena belum ada
koreksi untuk dilaporkan.

## Retensi

Audio sesi dan sidecar mengikuti retensi Fase A: bawaan 30 hari
(`audio.retention_days`), dihapus lewat `fakeyou audio purge-expired`.
Detail kebijakan ada di `docs/plans/phase-2-diarize-a.md`.
