# Spec capture per stream: transkrip beridentitas dari receiver

**Status:** spec disetujui pengguna, 6 Oktober 2026. Branch
`feat/per-stream-capture` dari `master` pada `66003d4`.
Rencana implementasi ditulis sesudah spec ini direview.

**Tujuan:** setiap receiver audio WebRTC Google Meet ditangkap
terpisah, ditranskrip pada stream STT sendiri, dan transkrip
lahir membawa identitas stream (slot + CSRC + jam). Overlap
dua pembicara menghasilkan dua teks bersih, bukan satu teks
rusak. Penamaan mengikuti dari identitas stream lewat binder.

**Latar:** capture mixed mono mencapai langit-langitnya pada
milestone nama speaker: gate Task 1 gagal di dua sesi live
([laporan](../reports/2026-10-06-speaker-names-gate.md)) karena
penanda DOM maksimal 0,93, eliminasi join cakupan 0, dan sinyal
mute DOM absen. Atribusi dari luar aliran audio tidak cukup;
identitas harus lahir bersama audio.

**Keputusan pengguna (6 Oktober):**

- Topologi STT: live per stream (satu stream Muse per receiver
  aktif), mengikuti pola Vexa (PCM per participant, stamp
  identitas saat capture, transkripsi downstream per identitas).
  Bedanya: stamp kami CSRC+slot+jam (sinyal terbukti kami),
  bukan nama glow (sinyal kami tidak stabil).
- Cakupan: 2–4 peserta. Penanganan slot rotation di atas itu
  di luar milestone.
- Migrasi: ganti total. Mixed dimatikan saat per-stream lulus
  acceptance; tanpa mode paralel-fallback.

## Bukti spike (6 Oktober, loopback offline, throwaway)

Dua nada (440Hz + 880Hz) lewat sepasang RTCPeerConnection,
diukur tap energi per receiver (uji di `/tmp`, tidak di-commit):

| Kondisi | Tap rx0 | Tap rx1 |
| --- | --- | --- |
| Dua nada nyala | 0,355 | 0,351 |
| Hanya 440Hz | 0,356 | 0 |
| Hanya 880Hz | 0 | 0,353 |

Nol bleed antar receiver. Ketiga API sadap bekerja:
WebAudio taps, `createEncodedStreams` per receiver (frame
mengalir dan terhitung), dan `MediaStreamTrackProcessor`
(151 AudioData, 72.480 sampel per receiver). Dua gotcha
wajib dipegang implementasi: track receiver harus ditempel
ke elemen `<audio>` yang `play()` (tanpa itu decode sunyi),
dan node audio harus disimpan referensinya (tanpa itu
garbage collector menghentikannya.

## Desain

### 1. Capture (JS in-page)

Init script (baru, pola mengikuti `assets/voice_activity.js`):
membungkus receiver saat `ontrack`, memasang AudioWorklet
PCM 16 kHz mono per receiver, dan drain per jendela 50 ms
membawa `{slot, csrcs, level, page_ms, pcm}`. Slot dilacak
lintas renegosiasi lewat CSRC yang menumpanginya (satu
CSRC peserta per slot, stabil per join — terbukti di riset
CSRC). Kehadiran receiver (aliran/hening) juga menjadi
sinyal mute per slot untuk eliminasi.

Batas: tanpa protokol privat Meet dan tanpa Meet Media API
(WebRTC standar saja); tanpa dependency baru; kompatibel
CloakBrowser container (bukan perilaku headless-shell uji).

### 2. Transkripsi dan identitas (Python)

Satu sesi per stream aktif: VAD → stream STT Muse →
`TranscriptEvent` membawa `stream_id`, CSRC, dan cap jam
mono yang sama dengan journal. Binder CSRC↔device yang
sudah ada (eliminasi join/mute/penanda, ikat-sekali,
unknown saat ambigu) berjalan di atas identitas stream;
slot→device mengikuti jejak CSRC. Merge kronologis per
stempel mono menghasilkan satu urutan transkrip untuk
agent; ingestion dan debrief membaca identitas dari stream,
bukan menebak ulang.

Input model tidak berubah pada milestone ini. Aturan
kewenangan persona, isolasi memori, dan c04 tidak berubah.

### 3. Cutover dan acceptance

Mixed dimatikan total saat acceptance lulus (keputusan
pengguna). Sebelum itu kedua jalur boleh hidup berdampingan
di branch ini untuk perbandingan, tetapi laporan acceptance
mengukur jalur per-stream saja.

Acceptance (dua sesi live container, 2–4 manusia):

- overlap terpisah menjadi dua teks yang benar ≥95%;
- atribusi solo ke stream yang benar ≥95%;
- nama (bila binder mengikat) presisi ≥0,98, sisanya
  `unknown` — tidak ada nama salah yang lolos;
- latency akhir ucapan → PCM pertama p50 ≤3 dtk, p95 ≤6 dtk;
- semua gate lama dipertahankan: replay probe-attr-8 dan
  csrc-live-3, gate semantik, laju c04, operasi worker;
- tanpa `xfail`, tanpa melonggarkan gate lain.

## Kontrak dan batasan global

- Python Inggris tanpa komentar/docstring dan tanpa string
  Bahasa Indonesia di `.py`; dokumentasi Bahasa Indonesia.
- Journal hanya metadata (`_ms`, kosakata `taxonomy.py`);
  tanpa teks transkrip, audio, prompt, atau secret.
- Capture 16 kHz mono per stream; playback 24 kHz tetap.
- Konfigurasi dari `Settings` dengan flag
  `FAKEYOU_CONVERSATION__PER_STREAM_ENABLED` default mati
  sampai acceptance lulus; saat mati perilaku identik mixed.
- Setiap peran memakai key sendiri; tidak ada pinjaman key.
- Sesi live hanya di container worker; jangan pernah di
  browser Mac. `.env` hanya diubah setelah backup.
- Fixture baru disamarkan (Host/Guest/Twin).
- Tes merah sebelum implementasi; reviewer terpisah per
  task; tanpa push/merge.

## Peta file awal

| File | Peran |
| --- | --- |
| `src/fakeyou/assets/stream_capture.js` (baru) | sadap PCM + stamp per receiver |
| `src/fakeyou/stream_capture.py` (baru) | sesi per stream: VAD, STT, identitas |
| `src/fakeyou/audio.py` | terima PCM per stream selain monitor mixed |
| `src/fakeyou/conversation.py` | `TranscriptEvent` bawa `stream_id` + merge |
| `src/fakeyou/voice_activity.py` | binder slot/CSRC atas identitas stream |
| `src/fakeyou/settings.py` | flag `per_stream_enabled` |
| `src/fakeyou/cli/join.py` | wiring capture per stream |
| `tests/test_stream_capture.py` (baru) | regresi offline + replay |
| `docs/reports/YYYY-MM-DD-per-stream-*.md` | bukti spike lanjutan dan acceptance |

Rincian task dan langkah implementasi ada di rencana
implementasi (menyusul lewat skill writing-plans), bukan
di spec ini.

## Risiko

- Slot Meet bergilir di atas 3 peserta remote — di luar
  cakupan, tetapi desain stamp CSRC disiapkan menanganinya
  nanti.
- Biaya STT naik kelipatan pembicara aktif (disetujui).
- CPU browser naik (decode + AudioWorklet per receiver);
  diukur di acceptance, bukan diasumsikan.
- Renegosiasi tengah sesi me-reset receiver — slot
  diikat ulang lewat CSRC kontinyu.
