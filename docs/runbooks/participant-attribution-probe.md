# Runbook Probe Atribusi Participant

Probe merekam bukti atribusi pembicara ke artefak tanpa mengubah perilaku
produksi. Penamaan produksi belum dipasang; seluruh transkrip tetap
`unknown` sampai gate Task 1 lulus.

## Menyalakan probe

```sh
cp .env .env.backup-<tanggal>
```

Nyalakan flag berikut di `.env`:

```sh
FAKEYOU_CONVERSATION__ATTRIBUTION_PROBE_ENABLED=true
```

Jalankan `fakeyou join` seperti biasa. Saat flag mati, perilaku sama
persis seperti sebelumnya.

## Artefak sesi

Satu direktori record menghasilkan berkas berikut:

- `attribution-probe.jsonl`: sampel CSRC/SSRC per receiver audio,
  level audio per track dari `getStats`, tap energi RMS per sumber
  WebAudio, potret roster DOM beserta kandidat penanda bicara/bisu,
  tepi VAD beserta jam sampel audio, dan jangkar jam halaman lawan
  jam Python. Pada Meet saat ini CSRC/SSRC/getStats kosong dan
  tap WebAudio menjadi sinyal utama
  ([laporan probe](../reports/2026-10-05-attribution-probe.md)).
- Record meeting: transkrip final beserta teks dan cap waktu.
- Jurnal: `transcript_finalized` dan tepi audio.

## Skrip sesi probe live

Sesi butuh host dan sedikitnya satu manusia lain selain twin. Host
mencatat ground truth per giliran: nomor giliran, nama display
pembicara, jenis giliran (`solo` atau `overlap`), dan cuplikan kata
yang diucapkan. Urutan yang disarankan:

1. Giliran solo bergantian, sedikitnya 10 giliran per manusia.
2. Bergantian cepat tanpa jeda panjang.
3. Overlap: dua manusia bicara bersamaan sekitar 2 detik.
4. Bisu/buka bisu bergantian, termasuk satu manusia bisu penuh
   selama giliran manusia lain.
5. Keluar lalu gabung kembali oleh satu manusia.
6. Nama display mirip bila memungkinkan.

Tulis ground truth sebagai JSON:

```json
[
  {"turn": 1, "speaker": "Rani", "kind": "solo", "fragment": "pagi"},
  {"turn": 2, "speaker": "Rani dan Budi", "kind": "overlap", "fragment": "bersama"}
]
```

Cuplikan dicocokkan tanpa memperhatikan kapital ke teks transkrip
secara berurutan.

## Mengukur gate

Jalankan ringkasan atas tiga berkas sesi:

```sh
.venv/bin/python -c "
from pathlib import Path
from fakeyou.audio.probe_replay import summarize_probe_session
import json
report = summarize_probe_session(
    Path('record-dir/attribution-probe.jsonl'),
    Path('record-dir/record.jsonl'),
    Path('truth.json'),
)
print(json.dumps(report, indent=2))
"
```

Laporan memuat lag CSRC terhadap VAD (median/p95), himpunan sumber
per pembicara, akurasi tiap kandidat penanda DOM, eliminasi bisu,
presisi aturan acuan, dan verdict gate. Gate lanjut ke Task 2
diukur atas giliran solo: presisi nama ≥98%, overlap menjadi
`unknown` ≥95%, label anonim stabil dalam satu sesi ≥95%. Bila
gate gagal, laporkan dan berhenti tanpa memasang penamaan produksi.

## Referensi

- [Rencana atribusi participant](../plans/participant-attribution.md)
- [Laporan gate media 3 Oktober](../reports/2026-10-03-media-gate.md)
