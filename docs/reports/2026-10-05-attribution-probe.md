# Laporan Probe Atribusi Participant — 2026-10-05 (GATE GAGAL)

Task 1 dari [rencana atribusi participant](../plans/participant-attribution.md):
mengukur apakah bukti berlapis cukup untuk penamaan produksi sebelum
dipasang. Sesi probe live dengan host, satu manusia lain, dan twin
pasif `AttrProbe` dalam container worker.

**Verdict: GATE GAGAL — jangan memasang penamaan produksi.**
Pemisahan sumber suara terbukti kuat lewat tap energi WebAudio, tetapi
tidak ada sinyal independen yang mengikat sumber ke nama: penanda
bicara DOM tidak ada, mute tidak pernah dipakai, dan penanda diri
struktural tidak dirender Meet. Presisi nama produksi tidak dapat
diukur, apalagi lulus ≥98%.

## Sesi probe

| Sesi | Fokus | Hasil |
| --- | --- | --- |
| probe-attr-1 | CSRC/SSRC | 34 ribu sampel, semuanya kosong |
| probe-attr-2 | level `getStats` | 0 level; receiver terlihat, topologi Meet tak memberi data |
| probe-attr-3/4 | ulang | gagal join transien (~10 dtk, tanpa detail); sesi-5 berikutnya masuk normal |
| probe-attr-5 | diagnostik `getStats` | `getStats` sukses tetapi 0 entri di 8 track; 34 transkrip |
| probe-attr-6 | sensus elemen media | 0 elemen `<audio>/<video>`; 2 peer, 4 receiver live |
| probe-attr-7 | topologi audio | 2 konteks audio, 6 sumber media WebAudio; `getStats` level peer juga 0 |
| probe-attr-8 | skrip 38 giliran | tap energi WebAudio memisahkan 2 pembicara; 44 transkrip |
| probe-attr-9/10 | DOM mentah | 148 tile berlabel: nol token pembeda bicara/diam; nol penanda diri |

Dua link Meet dipakai (pergantian di tengah jalan); ground truth
`truth-attr-8.json` (38 giliran: 16 verifikasi "Ini suara A/B",
20 skrip bernomor, 1 overlap, 1 rejoin) beserta generatornya
tersimpan di `.runtime/participant-attribution/`.

## Temuan bukti

- **CSRC/SSRC/getStats lumpuh total.** `getContributingSources`,
  `getSynchronizationSources`, `receiver.getStats()`, dan
  `peer.getStats()` semuanya kosong sementara media mengalir normal
  (STT bekerja). (Dikoreksi: stats kosong karena bug pembaca probe;
  sources kosong karena jalur insertable streams Meet. Lihat
  [riset CSRC](2026-10-05-csrc-research.md).)
- **Tidak ada elemen media.** Meet merutekan audio lewat WebAudio
  (2 AudioContext, 6 MediaStreamSource per sesi), bukan elemen
  `<audio>` per peserta. Pola capture per elemen Vexa tidak
  berlaku di sini.
- **Tap energi WebAudio berhasil.** AnalyserNode pasif per
  MediaStreamSource memberi level RMS per sumber: 17/17 giliran
  skrip berjarak normal tepat, rasio dominan lawan bocor 5–50x,
  overlap terdeteksi (1/1, tipis). Satu mikrofon B menangkap A
  pelan (ruang sama), ditangani aturan dominan bermargin 0,5
  ala resolver Zoom. Meet memakai 2 konteks audio dan 6 sumber
  media per sesi pada pengamatan ini.
- **Track didaur ulang saat rejoin.** (Dikoreksi di
  [addendum](#koreksi-atas-temuan-awal): indeks tap adalah slot
  audio Meet yang berpindah antar-join, bukan identitas.)
- **Rename teramati.** B berganti nama display dua kali
  (`Mela` → `m` → `n`) dalam satu sesi. (Dikoreksi: ini tiga
  join tamu terpisah, bukan rename di dalam panggilan.)
- **Penanda bicara DOM tidak ada.** Kandidat rantai LMA (ES310d)
  0/36, kelas dikenal Vexa 0/36, pindaian `aria-label` kosong,
  dan diff HTML tile (akar + anak awal) antara bicara dan diam
  tidak memberi satu token pun. Konsisten dengan temuan Vexa
  bahwa glow DOM tidak pernah menyala pada produksi.
- **Penanda diri struktural tidak dirender.** `data-self-name`
  dan `data-self` tidak muncul di HTML tile mana pun; seluruh
  tile terbaca `self=false` termasuk twin. Pengecualian diri
  Task 4 tidak dapat mengandalkan penanda ini; sinyal
  pengganti yang tersedia adalah korelasi playback TTS milik
  twin (diketahui dari jurnal).
- **Lag tap lawan VAD median −781 ms.** (Dikoreksi di
  addendum: angka ini artefak metrik onset; lag pipa sebenarnya
  sekitar 200 ms.)
- **Enam miss konsistensi sumber, semuanya terjelaskan.**
  Dua jendela VAD serpihan 0,35 detik yang salah petik atau
  kosong energi pada giliran berjarak <2 detik; satu transkrip
  STT menggabung dua pembicara (tak tereduksi pada granularitas
  transkrip); satu jendela batas sekuen yang merentang giliran
  sebelumnya; satu giliran ambigu (dominan kuat di sumber A
  sementara truth menyebut B — artefak truth atau jendela, tak
  dapat dipastikan tanpa audio); satu artefak rename (tap 4
  dipakai B sebelum dan sesudah rejoin, mayoritas suara
  menempel ke nama lama).

## Metrik replay (sesi probe-attr-8, 37/38 giliran selaras)

| Metrik | Nilai | Gate | Verdict |
| --- | --- | --- | --- |
| Konsistensi sumber solo (aturan dominan) | 0,833 (30/36) | presisi nama ≥0,98 | GAGAL |
| Skrip berjarak normal saja | 1,000 (17/17) | — | info |
| Overlap menjadi unknown | 1,000 (1/1) | ≥0,95 | TIPIS (n=1) |
| Stabilitas label anonim | 0,912 (31/34) | ≥0,95 | GAGAL |
| Akurasi kandidat DOM | 0,000 (0/36) | — | tidak ada sinyal |
| Eliminasi mute menjaga pembicara | 1,000; unik 0,000 | — | tak terukur (tak ada mute) |
| Lag tap vs VAD median/p95 | −781/−20 ms | — | info (delay pipa) |

Angka "konsistensi sumber" memakai ground truth untuk memetakan
sumber ke pembicara, sehingga BUKAN presisi nama produksi.
Presisi nama produksi tidak terdefinisi tanpa sinyal pengikat
nama, dan gate dinyatakan gagal.

## Dampak milestone

- Berhenti sesuai rencana: tidak ada penamaan produksi, tidak
  ada Task 2–6 atas dasar bukti ini.
- Yang terbukti dan dapat dipakai ulang: tap energi WebAudio
  sebagai aktivitas per sumber, aturan dominan bermargin,
 metode replay atas ground truth, dan runbook probe.
- Untuk atribusi nama ke depan, salah satu harus ada: sinyal
  korelasi nama yang belum ditemukan (butuh perburuan DOM
  tingkat halaman penuh, bukan level tile), STT diarization
  dari provider, atau STT per stream. Masing-masing adalah
  milestone sendiri.
- Label anonim (Speaker A/B) realistis dari bukti ini bila
  milestone dipersempit: sumber terpisah bersih dan stabil
  lintas rejoin.

## Risiko dan catatan

- Join worker gagal transien 2x beruntun lalu normal; kemungkinan
  throttle Meet atas join cepat berulang. Belum diselidiki
  sampai akar karena sesi berikutnya masuk.
- `.env` sesi dikembalikan dari backup; flag probe mati lagi.

## Addendum riset perisai WebRTC CloakBrowser

Addendum versi awal menyimpulkan CloakBrowser memasang perisai stats
tanpa syarat. Kesimpulan itu salah:

- Loopback-nya gagal karena policy ICE CloakBrowser (0 candidate, koneksi
  tak pernah terbentuk). `--webrtc-ip-handling-policy=default`
  memulihkannya.
- "getStats kosong" di Meet adalah bug pembaca probe.
- CSRC per peserta ternyata tersedia lewat metadata frame terenkode.

Rinciannya ada di [laporan riset CSRC](2026-10-05-csrc-research.md).

## Koreksi atas temuan awal

- **Indeks tap = slot audio Meet, bukan orang.** Ada tiga slot
  remote (indeks 3, 4, 5); indeks 0–2 mendekati nol. Timeline
  roster menunjukkan B keluar dan masuk lagi sebagai tamu baru
  dengan device berbeda (`devices/454` Mela → `455` m →
  `456` n). Slot B berpindah 4 → 5 → 4. Kembalinya B ke slot 4
  hanya kebetulan daur ulang slot. Label dari slot hanya sah dalam
  satu join, sesuai pelajaran Vexa bahwa slot bukan identitas.
- **Lag pipa ≈ 200 ms, bukan 781 ms.** Pemindaian kesesuaian
  antara aktivitas tap (≥0,01) dan status VAD memberi geseran
  terbaik 200 ms (kesesuaian 0,882; 150 ms 0,878; 250 ms 0,881),
  sama dengan `start_secs`/`stop_secs` Silero 0,2 detik. Angka
  −781 ms berasal dari metrik "sampel aktif pertama dalam
  [mulai − 1000 ms, akhir]" yang bias ke jendela lead.
- Implementasi label anonim yang memakai koreksi ini dilaporkan di
  [laporan label suara](2026-10-05-voice-labels.md).
