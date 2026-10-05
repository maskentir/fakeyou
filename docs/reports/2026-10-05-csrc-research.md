# Riset CSRC di CloakBrowser — 2026-10-05

Koreksi atas addendum di [laporan probe](2026-10-05-attribution-probe.md)
yang menyimpulkan CloakBrowser memasang perisai stats WebRTC tanpa syarat.
Kesimpulan itu salah.

**Verdict: CloakBrowser bisa seperti Chromium. CSRC per peserta tersedia di
Meet, di dalam container worker, lewat metadata frame audio terenkode.**

## Ringkasan temuan

1. **"getStats kosong" adalah bug probe.** `statEntries()` mengecek
   `report instanceof Map`, padahal `RTCStatsReport` maplike tetapi bukan
   `Map`. Probe lalu jatuh ke `Object.values()`, yang selalu `[]`.
   - Setelah diperbaiki (iterasi `forEach`), Meet di container memberi 37
     entri stats per peer.
   - `receiver.getStats()` berisi `inbound-rtp` dengan `audioLevel` per
     track (3816 baris dalam satu sesi).
2. **ICE CloakBrowser dibatasi policy, bukan dimatikan.**
   - Default-nya JS menerima 0 candidate, dan loopback lokal tetap di
     state `new` (sama dengan issue
     [#543](https://github.com/CloakHQ/CloakBrowser/issues/543)).
   - Tes loopback lama gagal karena koneksi tidak pernah terbentuk, bukan
     karena stats diblokir.
   - `--webrtc-ip-handling-policy=default` mengembalikan perilaku Chromium,
     baik di Mac 145.0.7632.109.2 maupun di Linux container
     146.0.7680.177.3: koneksi `connected`, `inbound-rtp` terisi, SSRC
     `audioLevel` 0,708.
   - `--force-webrtc-ip-handling-policy=default` sendirian tidak berpengaruh.
   - Di Meet, koneksi tetap jalan tanpa flag lewat ICE-TCP (`host/tcp` dan
     `prflx/tcp` di Mac).
3. **`getSynchronizationSources`/`getContributingSources` kosong di Meet
   container karena jalur media Meet sendiri.**
   - Hasilnya 0 dari 11.191 sampel, padahal RTP mengalir.
   - Meet memanggil `RTCRtpReceiver.createEncodedStreams()` 4 kali (insertable
     streams) dan membuat 2 `AudioWorkletNode`.
   - Track yang masuk WebAudio tidak sama dengan track receiver (0 dari 6
     overlap).
   - Percobaan menempelkan track ke `<audio>` senyap tidak membantu di Meet,
     jadi dibuang.
   - Di Mac headed, kedua API itu terisi (SSRC `6666`, CSRC `42` dan
     `1416569104`). Bedanya belum dijelaskan; sesi itu tidak mengukur
     pemakaian insertable streams.
4. **Metadata frame terenkode memberi identitas dan energi.**
   - Probe membungkus `createEncodedStreams()` dan melewatkan `readable`
     milik Meet melalui `TransformStream` pasif. Frame diteruskan utuh.
   - Tiap frame (±20 ms) membawa:
     - `synchronizationSource`: slot virtual Meet;
     - `contributingSources`: SSRC peserta yang sedang dirutekan ke slot;
     - `audioLevel`: level dari header extension RTP. Ini terverifikasi di
       loopback lokal, tetapi belum direkam di Meet live.
   - Ini metadata WebRTC standar (`RTCEncodedAudioFrameMetadata`), bukan
     protokol privat Meet.
5. **Hasil live dua orang (probe-csrc-7, container):**

   | Slot (receiver) | SSRC virtual | CSRC peserta |
   | --- | --- | --- |
   | 0 | `6666` | `1416569104` (A), ditambah `42` |
   | 1 | `6667` | `3231413436` (B), ditambah `42` |

   - Tiap slot hanya membawa satu CSRC peserta sepanjang sesi.
   - `42` muncul di semua slot dan diperlakukan sebagai penanda konstan Meet,
     bukan orang.
   - Kehadiran frame per CSRC belum cukup untuk menentukan siapa yang
     bicara. Saat B bicara, frame A ikut mengalir (porsi ±0,5), kemungkinan
     bocoran mic karena satu ruangan. Penentu bicara harus memakai
     `audioLevel` per frame, yang kini direkam probe.

## Sesi live

| Sesi | Isi | Hasil |
| --- | --- | --- |
| Mac headed (diag terpisah) | CloakBrowser default di Meet | `getStats` 41 entri; SSRC/CSRC terisi; peserta mendengar gema, jadi tidak diulang di Mac |
| probe-csrc-3 | reader stats diperbaiki | stats lengkap; SSRC/CSRC 0; satu fault STT `no_transcripts` lalu reconnect |
| probe-csrc-4 | render senyap | `blocked_by_platform` dalam 10 detik setelah join beruntun; retry 60 detik kemudian masuk |
| probe-csrc-5 | render senyap | SSRC/CSRC tetap 0; track WebAudio ≠ track receiver |
| probe-csrc-6 | penghitung jalur media | `createEncodedStreams` 4×, `AudioWorkletNode` 2×, tanpa transform/generator |
| probe-csrc-7 | keran frame terenkode, 2 orang | SSRC per slot, CSRC per peserta |

## Label suara anonim pada sesi live yang sama

Cek awal, belum acceptance. Pada probe-csrc-7 ada 9 transkrip: 5 berlabel
dan semuanya benar (A tiga kali, B dua kali), 4 `unknown(overlap)`, dan 0
salah. Overlap tinggi konsisten dengan bocoran antar-mic di satu ruangan.

## Dampak

- Penyatuan label lintas slot, dan lebih dari 3 peserta, menjadi mungkin
  tanpa embedding suara. Pembicara adalah CSRC dari slot yang dominan
  menurut `audioLevel` frame. CSRC stabil per join peserta, dan tidak
  bergantung pada slot.
- Nama tetap butuh pengikatan CSRC ke device roster (eliminasi seperti
  binder slot) dan harus lolos gate presisi 0,98 sebelum dinyalakan.
- Risiko:
  - Membungkus `createEncodedStreams` menambah satu hop JS per frame di
    jalur media Meet. Sesi probe-csrc-7 tetap normal (audio, STT, dan
    jawaban twin jalan), tetapi belum diuji lama.
  - Pembungkus prototype bisa terdeteksi.
  - Makna CSRC `42` belum diketahui.
- Langkah berikut yang direncanakan kini sudah dijalankan; lihat
  [label CSRC live](#label-csrc-live-csrc-live-3).

## Label CSRC live (csrc-live-3)

Sumber label suara kini memakai CSRC. Setelah frame ber-CSRC terlihat,
tracker berpindah ke mode CSRC:

- `voice_activity.js` merekam per jendela 50 ms per slot receiver: level
  maksimum dan daftar CSRC dari metadata frame.
- Pembicara adalah CSRC dari slot yang dominan menurut level frame. Aturan
  margin 0,5 dan ambang 0,01 sama dengan mode slot.
- CSRC yang muncul bersama dua CSRC lain atau lebih (seperti `42`)
  dianggap penanda dan diabaikan.
- Label diikat ke CSRC, jadi tetap sama bila Meet memindah slot, dan
  bertahan saat halaman reload.
- Dua CSRC berlabel berbeda, atau campuran CSRC berlabel dan baru dalam satu
  jendela, menjadi `unknown(csrc_conflict)`.
- Mode slot WebAudio tetap dipakai bila Meet tidak membawa CSRC.

Sesi live dengan dua orang di ruangan berbeda menghasilkan:

| Metrik | Live | Replay (`test_csrc_label_replay_gate_on_live_session`) |
| --- | --- | --- |
| Transkrip solo berlabel benar | 13/13 (giliran 3 terpecah dua) | 11/11 giliran selaras |
| Stabilitas per orang | 1,0 | 1,0 |
| Purity | 1,0 | 1,0 |
| Overlap menjadi `unknown` | 2/2 | 2/2 |
| Label per orang | A satu, B satu | `{"A": ["speaker-a"], "B": ["speaker-b"]}` |
| Label dari suara twin | 0 | — |

- CSRC A sama dengan sesi sebelumnya karena A tidak keluar meeting. CSRC B
  berganti karena B join ulang. Jadi CSRC stabil per join.
- Lag frame terhadap VAD: lead onset median 130 ms (p25 54, p75 519), dan
  puncak kesesuaian sekitar 700 ms dengan plato 550–800 ms.
- Replay memberi hasil identik untuk lag 200–800 ms. `FRAME_LAG_MS = 300`
  dipertahankan sebagai nilai yang tervalidasi live di tengah rentang itu.
- Fixture `tests/fixtures/voice-labels-csrc-live-3.json` berisi nama
  disamarkan (Host/Guest/Twin), device id dinomori ulang, obrolan di luar
  script dikosongkan, dan tanpa level WebAudio.

Temuan sampingan dari sesi ini:

- Tahap "Getting ready…" (verifikasi bot) Meet kini makan ±8–10 detik.
- Alur join hanya menunggu kolom nama ±9 detik, sehingga dua join gagal
  (`clicking 'name field' failed`). Anggaran tunggu kolom nama dinaikkan
  menjadi 10 percobaan (±30 detik); join berikutnya masuk dalam 28,8 detik.
- `join_finished` di journal kini membawa `detail`. Sebelumnya alasan gagal
  hanya tersimpan di checkpoint.

## Verifikasi kode

- Tes browser baru merah sebelum perubahan dan hijau sesudahnya:
  - `test_probe_counts_entries_of_a_real_stats_report`;
  - `test_probe_reads_identity_and_level_from_encoded_audio_frames`.
- `pytest -m browser tests/test_attribution_probe.py`: 7 lolos.
- Tes browser `test_voice_script_reads_level_and_csrc_from_encoded_frames`
  memastikan script produksi membaca level dari frame terenkode.
