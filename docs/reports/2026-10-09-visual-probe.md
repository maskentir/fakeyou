# Laporan Probe Visual Meet — 2026-10-09 (LULUS)

Probe Task 0 dari [rencana konteks visual](../plans/visual-context.md):
mengunci pemilih tile presentasi Meet asli dan mengukur TTFT
gambar-di-prompt sebelum angka gate dibekukan.

**Verdict: GATE LULUS — Tugas 1 boleh mulai.**

## Sesi probe

- Twin probe `M6Probe`; manusia share satu tab browser (±6 menit).
- Skrip sekali-pakai di `/tmp` (tidak di repo): join tamu, pindai
  aria-label penuh, tunggu share terdeteksi, pindai 2x + screenshot
  full-page, tunggu stop, pindai sesudah, leave.
- Artefak mentah: `/tmp/m6probe/out/probe_visual.json`
  (`s0` baseline, `s1`+`s1b` saat-share, `s2` sesudah-stop) +
  `share_full.png`. Catatan: manusia sudah share sebelum `s0`,
  sehingga beda valid adalah `s1` vs `s2` (bukan `s0`).
- Run pertama: admit tapi tidak ada share dalam 6 menit (manusia
  belum siap); run kedua: admit + share + stop + leave penuh.

## Selector terkunci (teramati, 2x pindai sepakat)

Bedakan `s1` (share) vs `s2` (stop); `s1==s1b` identik:

- `"<Nama> is presenting"` — cocokkan sufiks `is presenting`
  (case-insensitive); nama dinamis, tidak pernah sama-persis.
- `"Presentation"` — label persis, independen-nama; kandidat utama.
- `"Unpin <Nama>'s presentation from your main screen"` — konfirmasi
  sekunder (tile ter-pin).
- Kontrol satelit saat share: `Enter Full Screen`,
  `Open in new window`, `Zoom in`, `Pin <Nama> to your main screen`.
- Tidak ada atribut `data-is-presenting`/`data-presenting` (0 hit);
  tidak dipakai.
- Screenshot full-page menangkap konten share dengan terbaca
  (aktual 937x935); klip tile dipersempit di Tugas 1.

## TTFT gambar-di-prompt (jalur produksi, host)

Model live `gemini-3.8-flash` via `google_genai`, standard image
blocks LangChain, 1 frame JPEG 640x360 + 1 kalimat, 5 invoke:

- invoke1–5 TTFT: 2,58 / 2,94 / 2,97 / 4,31 / 4,23 detik
  (angka owner-observed; stdout mentah tidak disimpan).
- 5/5 OK, p50 2,97 detik, maks 4,31 detik — maks < 12 sehingga
  p95 sampel mana pun < 12 (p95 formal tak dihitung pada n=5).
  Angka gate BEKU.
- Jawaban membaca slide dengan benar; `usage_metadata` tercatat
  (1115/125 token pada run bukti awal).

## Kontrak untuk Tugas 1+

1. Deteksi share = label `is presenting` (sufiks-i) ATAU label
   persis `Presentation`; debounce 2 pindai identik.
2. Screenshot elemen tile bila locator stabil, else full-page +
   klip di Tugas 1.
3. Kadensi default 1 frame/2 detik; dedup hash sebelum VLM.
