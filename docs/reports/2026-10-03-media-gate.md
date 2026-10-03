# Laporan Gate Media Multipihak — 2026-10-03 (NEGATIF)

Gate media Task 0 dari [rencana multipihak](../plans/multiparty-participation.md):
menentukan apakah awal/akhir audio per transkrip dapat dikorelasikan ke
timeline speaker dalam epoch yang sama memakai satu clock, sebelum
penamaan produksi dipasang.

**Verdict: GATE GAGAL — atribusi produksi mempertahankan `unknown`.**
Tidak ada adapter private atau migrasi per-channel dalam milestone ini,
sesuai batasan plan. Policy pure dan routing konservatif tetap dapat
dipasang; fitur atribusi live dinyatakan belum selesai.

## Sesi probe

- Link Meet uji baru dari host; 2 manusia (`Harun Nur Rasyid`,
  `biya 28`, dikonfirmasi host) + twin pasif `MediaTwin`.
- `probe-m9`: 11 transkrip (gantian, overlap, gantian cepat,
  leave/rejoin O2); 354 snapshot DOM @250 ms.
- `probe-m10`: 3 transkrip gantian bersih; 150 snapshot dengan
  cuplikan HTML tile.
- Commit probe: `f4634ef`; image cocok hash.

## Temuan sumber (pembacaan kode)

- `TranscriptionFrame.timestamp` diisi `time_now_iso8601()` pada saat
  frame dibuat = waktu tiba dinding, bukan interval audio sumber.
- `user_id` frame = ID statis service, bukan speaker. Label speaker
  hanya ada pada log trace mode diarization, tidak sampai ke frame.
- `pts` tidak diset oleh transport audio lokal (hanya smallwebrtc);
  frame pipeline tidak membawa jam sumber.
- Capture mono campuran; tidak ada channel per participant.
- Tidak ada source Vexa lokal; probe dilakukan langsung pada DOM.

## Temuan DOM (teramati, bukan tebakan)

- Roster: atribut `data-participant-id` stabil per join dengan bentuk
  `spaces/<space>/devices/<id>`; ID baru diterbitkan saat rejoin
  (`.../537` → `.../545` tepat pada rejoin O2). Nama display terbaca
  pada teks tile.
- Penanda pembicara-aktif: TIDAK DITEMUKAN pada fondasi yang
  diterima. Pindaian seluruh `aria-label` halaman tidak menemukan
  satupun label `speak`; class dan aria tile tidak berubah antara
  ucapan dan sunyi.
- Satu-satunya perbedaan teramati ialah pertukaran token class
  obfuscated (`sxlEM` ↔ `h8UR3d`) pada elemen
  `jscontroller="YQvg8b" jsname="QgSmzd"`. Korelasinya lemah
  terhadap jendela VAD (Harun tp=24 fp=12 fn=20; biya tp=15 fp=3
  fn=29; twin kontrol 0/0) dan plan menolak CSS obfuscated sebagai
  fondasi. Tidak dipakai.
- Marker self `[data-self="true"]` tidak pernah cocok; tile twin
  hanya dikenali lewat teks nama + kontrol self-view (bukan key).
- Panel `aria-label="People"` tidak ditemukan.

## Kontrak yang hilang

1. Sinyal aktivitas speaker per ID pada clock yang sama dengan audio.
2. Jangkar jam sumber untuk interval transkrip (`audioProcessedMs`
   pada pesan STT belum divalidasi dan tidak dicatat journal).
3. Tanpa (1), `SpeechWindow` tidak dapat diikat ke timeline speaker;
   resolver benar menurut kontrak tetapi tidak pernah menerima
   evidence yang cukup pada produksi.

## Dampak milestone

- Produksi mempertahankan `unknown` untuk seluruh atribusi live.
  Tidak ada penamaan, tidak ada klaim channel per participant.
- Tetap boleh dipasang: policy pure reply/silent/clarify di atas
  alamat eksplisit (grammar vocative), floor occupied dari VAD,
  follow-up unknown → silent; field record; laporan keputusan;
  flag pilot.
- Gate atribusi Task 5 tetap belum terbukti walaupun policy lulus.
  Kebutuhan desain berikut: binding capture per-channel atau SDK
  yang menyediakan activity-level per participant.
