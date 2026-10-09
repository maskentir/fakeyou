# Laporan Probe DOM Tindakan Meet — 2026-10-09 (LULUS)

Probe Task 0 dari [rencana tindakan Meet](../plans/meet-actions.md):
mengunci pemilih DOM Meet asli untuk tombol tangan, status tangan
milik sendiri, panel chat, kolom input chat, dan gema pesan terkirim,
sebelum implementasi apa pun.

**Verdict: GATE LULUS — Tugas 1, 4, dan 5 boleh mulai.**

## Sesi probe

- Link Meet dari host; twin probe `M4Probe` (1 join, dalam throttle
  identitas ~5 join/jam).
- Skrip sekali-pakai di `/tmp` (tidak di repo): join tamu, pindai
  seluruh `aria-label` halaman, klik raise hand, pindai ulang 2x,
  klik lower hand, buka panel chat, ketik + Enter satu pesan
  (`M4 probe ping <ts>`), pindai gema 2x, leave.
- Artefak mentah: `/tmp/m4probe/out/probe_task0.json` (7 cuplikan:
  `s0` dalam-panggilan, `s1`+`s1b` tangan-terangkat, `s2` tangan-turun,
  `s3` chat-terbuka, `s4`+`s4b` gema). Cuplikan ulang identik
  (`s1==s1b`, `s4==s4b`): tiap pemilih lolos 2x pemindaian berurutan.
- Dua run awal timeout di lobi (tidak di-admit; bukti: log run-2
  `/tmp/m4probe/run2.log` mencatat `JOIN timeout`); run ketiga admit +
  selesai penuh sampai leave.

## Jawaban gate (teramati, bukan tebakan)

- **G0-1 (atribut tombol tangan):** `aria-label` persis
  `Raise hand (ctrl + alt + h)` dan `Lower hand (ctrl + alt + h)` —
  akhiran pintasan termasuk label, jadi pencocokan wajib prefiks
  case-insensitive, tidak pernah sama-persis. `aria-pressed` TIDAK
  hadir (`null` sebelum dan sesudah) — dilarang dipakai observasi.
- **G0-2 (observasi status tangan sendiri):** tukar label adalah
  satu-satunya beda atribut aria (`+Lower hand…`, `-Raise hand…`).
  Mekanisme observasi = label swap. Teks tray ikut terekam berubah
  (`Call feature notifications…` memuat nama + `front_hand`) tetapi
  tidak dipakai observasi; ikon tile tidak dipindai (tidak perlu —
  label swap bersih).
- **G0-3 (kirim chat + gema):** Enter pada textarea mengirim; gema
  pesan sendiri muncul di daftar pesan (teks `M4 probe ping …`
  terbaca-balik). Tombol kirim tidak diuji (Enter cukup).
- **G0-4 (panel terbuka-tertutup):** label tombol chat konstan
  `Chat with everyone` baik terbuka maupun tertutup — deteksi buka
  wajib kehadiran `textarea[maxlength="4000"]` (0 → 1 area).
- **G0-5 (pintasan non-macOS):** `ctrl + alt + h` (dari akhiran label).
- **G0-6 (baca-balik gema):** terverifikasi — teks sendiri muncul
  (plus satu label `Pin message` baru pada aria).
- **G0-7 (seed selector terkunci):**
  - tangan: `button` dengan prefiks aria `raise hand` / `lower hand`
    (flag `i`);
  - chat: `button` prefiks aria `chat` + `textarea[maxlength="4000"]`
    untuk input dan deteksi panel.

## Kontrak untuk Tugas 1/4/5

1. Semua pencocokan label aria memakai prefiks + flag `i`.
2. Kolom chat dicocokkan lewat struktur
   (`textarea[maxlength="4000"]`), bukan `aria-label` (labelnya
   `Send a message`, rawan berubah).
3. Klik humanized memakai `xpath=`, selalu tunggu-visible dulu.
4. Fondasi ditolak tetap ditolak: tanpa `aria-pressed` tangan (tidak
   ada), tanpa class obfuscated, tanpa `[data-self]`, tanpa glow DOM.
